"""教程上传和受控阅读接口，复用身份、CSRF、场景仓储及文件服务。"""

import asyncio
import re
from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import current_actor, require_admin
from app.modules.applications.tutorial_documents import VIDEO_LIMIT
from app.modules.applications.tutorial_files import readable_file, save_file
from app.storage.client import ObjectNotFound, StorageDependency

router = APIRouter(tags=["application tutorials"])


class TutorialUploadLimit(MultiPartException):
    """触发 multipart 的临时文件清理并报告大小限制。"""


@router.post("/application-tutorial-files", status_code=201)
async def upload_tutorial(request: Request, storage: StorageDependency) -> dict:
    """先验证发布权限，再限流解析和保存一个教程文件。"""
    actor = current_actor(request)
    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前账号没有上传教程的权限")
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
            if count > VIDEO_LIMIT + 64 * 1024:
                raise TutorialUploadLimit("视频不能超过 100 MiB")
            yield block

    try:
        parser = MultiPartParser(
            request.headers, limited_stream(), max_files=1, max_fields=1, max_part_size=4096
        )
        try:
            form = await parser.parse()
        except TutorialUploadLimit as error:
            raise HTTPException(413, str(error)) from None
        except MultiPartException:
            raise HTTPException(422, "上传表单无效，每次请选择一个教程文件") from None
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
    finally:
        if form is not None:
            await form.close()
        elif parser is not None:
            for temporary in parser._files_to_close_on_error:
                temporary.close()
        limiter.release()


def byte_range(value: str | None, size: int) -> tuple[int, int, int]:
    """解析浏览器单段 Range 请求，支持视频拖动及 PDF 按需读取。"""
    if value is None:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) <= 128 else None
    if match and (match[1] or match[2]):
        start = int(match[1]) if match[1] else max(0, size - int(match[2]))
        end = min(int(match[2]), size - 1) if match[1] and match[2] else size - 1
        if 0 <= start <= end < size:
            return start, end, 206
    raise HTTPException(416, "请求的文件范围无效", headers={"Content-Range": f"bytes */{size}"})


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
    key = file["previewKey"] if preview else file["objectKey"]
    media_type = file["previewContentType"] if preview else file["contentType"]
    try:
        info = storage.stat_object(key)
    except ObjectNotFound:
        raise HTTPException(404, "教程文件已不可用，请联系发布者重新上传") from None
    start, end, status = byte_range(request.headers.get("range"), info.size)
    length = end - start + 1
    disposition = "inline" if preview else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file['name'], safe='')}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
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

    return StreamingResponse(chunks(), status_code=status, media_type=media_type, headers=headers)
