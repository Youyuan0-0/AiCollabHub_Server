"""受控文件接口的传输工具；调用前须由业务路由完成身份和资源权限检查。"""

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser

from app.storage.client import MinioStorage, ObjectNotFound


class UploadLimitExceeded(MultiPartException):
    """请求体超限时触发 multipart 解析器的临时文件清理。"""


@asynccontextmanager
async def limited_upload_form(
    request: Request,
    limiter: asyncio.Semaphore,
    *,
    max_bytes: int,
    max_fields: int,
    too_large_message: str,
    invalid_form_message: str,
    max_part_size: int = 1024 * 1024,
) -> AsyncIterator[FormData]:
    """限流并有界解析单文件表单，退出或取消时释放文件及名额；字段规则由调用方检查。"""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(415, "请使用文件上传表单")
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    except TimeoutError:
        raise HTTPException(429, "当前上传较多，请稍后重试") from None

    count = 0
    form = None
    parser = None

    async def limited_stream() -> AsyncIterator[bytes]:
        """按实际接收字节计数，不信任客户端申报的 Content-Length。"""
        nonlocal count
        async for block in request.stream():
            count += len(block)
            if count > max_bytes:
                raise UploadLimitExceeded(too_large_message)
            yield block

    try:
        parser = MultiPartParser(
            request.headers,
            limited_stream(),
            max_files=1,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )
        try:
            form = await parser.parse()
        except UploadLimitExceeded as error:
            raise HTTPException(413, str(error)) from None
        except MultiPartException:
            raise HTTPException(422, invalid_form_message) from None
        yield form
    finally:
        try:
            if form is not None:
                await form.close()
            elif parser is not None:
                # 取消和断连不一定进入解析器的异常清理分支，仍需关闭已落盘的临时文件。
                for temporary in parser._files_to_close_on_error:
                    temporary.close()
        finally:
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


def object_response(
    request: Request,
    storage: MinioStorage,
    *,
    key: str,
    filename: str,
    media_type: str,
    preview: bool,
    missing_message: str,
    cache_control: str | None = None,
) -> Response:
    """返回已授权对象的完整或分段内容；保留业务指定的缓存策略和缺失提示。"""
    try:
        info = storage.stat_object(key)
    except ObjectNotFound:
        raise HTTPException(404, missing_message) from None
    start, end, status = byte_range(request.headers.get("range"), info.size)
    length = end - start + 1
    disposition = "inline" if preview else "attachment"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if cache_control is not None:
        headers["Cache-Control"] = cache_control
    if media_type == "text/html":
        headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "frame-ancestors 'self'; sandbox"
        )
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{info.size}"
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)

    def chunks() -> Iterator[bytes]:
        """流式读取指定范围，由存储上下文负责归还连接。"""
        with storage.open_object(key, offset=start, length=length) as stream:
            remaining = length
            while remaining:
                block = stream.read(min(64 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(chunks(), status_code=status, media_type=media_type, headers=headers)
