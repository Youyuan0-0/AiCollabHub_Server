"""应用审核报告上传、暂存列表、移除和受控预览接口。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import current_actor
from app.modules.applications.review_files import (
    check_pending,
    checked_publication,
    draft_reports,
    public_report,
    readable_report,
    remove_report,
    save_report,
)
from app.modules.applications.tutorial_documents import DOCUMENT_LIMIT
from app.modules.common.file_transfer import limited_upload_form, object_response
from app.storage.client import StorageDependency

router = APIRouter(prefix="/admin/application-publications", tags=["application review reports"])


@router.get("/{publication_id}/report-files")
def report_files(publication_id: str, request: Request) -> list[dict]:
    """读取当前审核员暂存附件；已提交的附件从审核历史返回。"""
    actor = current_actor(request, True)
    with application_service(request).repository.transaction(is_readonly=True) as session:
        record = checked_publication(session, publication_id, actor)
        return [public_report(file) for file in draft_reports(session, record, actor)]


@router.post("/{publication_id}/report-files", status_code=201)
async def upload_report(
    publication_id: str, request: Request, storage: StorageDependency, revision: int = Query(ge=1)
) -> dict:
    """身份和申请状态检查后限流接收一个文档，不接受浏览器自报对象地址。"""
    actor = current_actor(request, True)
    service = application_service(request)

    def check_access() -> None:
        with service.repository.transaction(is_readonly=True) as session:
            check_pending(checked_publication(session, publication_id, actor), revision)

    await run_in_threadpool(check_access)
    async with limited_upload_form(
        request,
        request.app.state.tutorial_upload_limiter,
        max_bytes=DOCUMENT_LIMIT + 64 * 1024,
        max_fields=0,
        too_large_message="报告文档不能超过 20 MiB",
        invalid_form_message="每次请选择一个报告文件",
    ) as form:
        file = form.get("file")
        if len(form.multi_items()) != 1 or not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "请选择一个报告文件")
        content = await file.read(DOCUMENT_LIMIT + 1)
        return await run_in_threadpool(
            save_report, service, storage, actor, publication_id, revision, file.filename, content
        )


@router.delete("/{publication_id}/report-files/{identifier}", status_code=204)
def delete_report(
    publication_id: str, identifier: UUID, request: Request, storage: StorageDependency
) -> Response:
    """移除当前管理员尚未提交的附件。"""
    remove_report(
        application_service(request),
        storage,
        publication_id,
        str(identifier),
        current_actor(request, True),
    )
    return Response(status_code=204)


@router.api_route("/{publication_id}/report-files/{identifier}/content", methods=["GET", "HEAD"])
def report_content(
    publication_id: str,
    identifier: UUID,
    request: Request,
    storage: StorageDependency,
    preview: bool = False,
) -> Response:
    """验证申请访问权限后流式返回原件或阅读预览，不暴露 MinIO 公共链接。"""
    file = readable_report(
        application_service(request), publication_id, str(identifier), current_actor(request, True)
    )
    if file["bucket"] != storage.bucket:
        raise HTTPException(503, "报告存储配置已变化，请联系管理员")
    return object_response(
        request,
        storage,
        key=file["previewKey"] if preview else file["objectKey"],
        filename=file["name"],
        media_type=file["previewContentType"] if preview else file["contentType"],
        preview=preview,
        missing_message="报告文件已不可用，请联系管理员",
        cache_control="private, no-store",
    )
