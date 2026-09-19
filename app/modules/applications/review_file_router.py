"""应用审核报告上传、暂存列表、移除和受控预览接口。"""

import asyncio
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

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
from app.modules.applications.tutorial_router import byte_range
from app.storage.client import ObjectNotFound, StorageDependency

router = APIRouter(prefix="/admin/application-publications", tags=["application review reports"])


class ReportUploadLimit(MultiPartException):
    """超限时让 multipart 解析器清理临时文件。"""


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
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "请使用文件上传表单")
    limiter = request.app.state.tutorial_upload_limiter
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    except TimeoutError:
        raise HTTPException(429, "当前上传较多，请稍后重试") from None
    count = 0
    form = None
    parser = None

    async def limited_stream() -> AsyncIterator[bytes]:
        nonlocal count
        async for block in request.stream():
            count += len(block)
            if count > DOCUMENT_LIMIT + 64 * 1024:
                raise ReportUploadLimit("报告文档不能超过 20 MiB")
            yield block

    try:
        parser = MultiPartParser(request.headers, limited_stream(), max_files=1, max_fields=0)
        try:
            form = await parser.parse()
        except ReportUploadLimit as error:
            raise HTTPException(413, str(error)) from None
        except MultiPartException:
            raise HTTPException(422, "每次请选择一个报告文件") from None
        file = form.get("file")
        if len(form.multi_items()) != 1 or not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "请选择一个报告文件")
        content = await file.read(DOCUMENT_LIMIT + 1)
        return await run_in_threadpool(
            save_report, service, storage, actor, publication_id, revision, file.filename, content
        )
    finally:
        if form is not None:
            await form.close()
        elif parser is not None:
            for temporary in parser._files_to_close_on_error:
                temporary.close()
        limiter.release()


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
    key = file["previewKey"] if preview else file["objectKey"]
    media_type = file["previewContentType"] if preview else file["contentType"]
    try:
        info = storage.stat_object(key)
    except ObjectNotFound:
        raise HTTPException(404, "报告文件已不可用，请联系管理员") from None
    start, end, status = byte_range(request.headers.get("range"), info.size)
    length = end - start + 1
    disposition = "inline" if preview else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file['name'], safe='')}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if media_type == "text/html":
        headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "frame-ancestors 'self'; sandbox"
        )
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{info.size}"
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)

    def chunks():
        with storage.open_object(key, offset=start, length=length) as stream:
            remaining = length
            while remaining:
                block = stream.read(min(64 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(chunks(), status_code=status, headers=headers, media_type=media_type)
