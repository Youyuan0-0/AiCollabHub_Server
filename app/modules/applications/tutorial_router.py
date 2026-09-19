"""教程上传和受控阅读接口，复用身份、CSRF、场景仓储及文件服务。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import current_actor, require_admin
from app.modules.applications.tutorial_documents import VIDEO_LIMIT
from app.modules.applications.tutorial_files import readable_file, save_file
from app.modules.common.file_transfer import limited_upload_form, object_response
from app.storage.client import StorageDependency

router = APIRouter(tags=["application tutorials"])


@router.post("/application-tutorial-files", status_code=201)
async def upload_tutorial(request: Request, storage: StorageDependency) -> dict:
    """先验证发布权限，再限流解析和保存一个教程文件。"""
    actor = current_actor(request)
    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前账号没有上传教程的权限")
    async with limited_upload_form(
        request,
        request.app.state.tutorial_upload_limiter,
        max_bytes=VIDEO_LIMIT + 64 * 1024,
        max_fields=1,
        too_large_message="视频不能超过 100 MiB",
        invalid_form_message="上传表单无效，每次请选择一个教程文件",
        max_part_size=4096,
    ) as form:
        if sorted(form.keys()) != ["file", "securityLevel"] or len(form.multi_items()) != 2:
            raise HTTPException(422, "请上传一个文件并标注资料密级")
        file = form.get("file")
        if not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "请选择教程文件")
        content = await file.read(VIDEO_LIMIT + 1)
        return await run_in_threadpool(
            save_file,
            application_service(request),
            storage,
            actor,
            file.filename,
            content,
            str(form.get("securityLevel", "")),
        )


@router.api_route("/application-tutorial-files/{identifier}/content", methods=["GET", "HEAD"])
@router.api_route("/admin/application-tutorial-files/{identifier}/content", methods=["GET", "HEAD"])
def tutorial_content(
    identifier: UUID,
    request: Request,
    storage: StorageDependency,
    publication_id: str | None = Query(default=None, alias="publicationId", max_length=200),
    application_id: str | None = Query(default=None, alias="applicationId", max_length=200),
    preview: bool = False,
) -> Response:
    """按当前草稿或目录访问权限提供原文件和可内嵌预览，禁止公开裸存储地址。"""
    is_admin = request.url.path.startswith("/api/v1/admin/")
    actor = current_actor(request, is_admin)
    if is_admin:
        require_admin(actor)
    file = readable_file(
        application_service(request),
        actor,
        str(identifier),
        publication_id,
        application_id,
        is_admin=is_admin,
    )
    return object_response(
        request,
        storage,
        key=file["previewKey"] if preview else file["objectKey"],
        filename=file["name"],
        media_type=file["previewContentType"] if preview else file["contentType"],
        preview=preview,
        missing_message="教程文件已不可用，请联系发布者重新上传",
    )
