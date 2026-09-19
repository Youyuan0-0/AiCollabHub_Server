"""公共传输工具的资源释放和分段读取回归，使用内存文件，不连接 MinIO。"""

import asyncio
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette import formparsers

from app.modules.common.file_transfer import limited_upload_form, object_response
from app.storage.client import ObjectInfo


@pytest.mark.parametrize("outcome", ["success", "business_error", "too_large", "cancelled"])
def test_upload_always_releases_temporary_file_and_limiter(monkeypatch, outcome):
    files = []
    original = formparsers.SpooledTemporaryFile

    def record_file(*args, **kwargs):
        file = original(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", record_file)
    first = (
        b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.txt"'
        b"\r\nContent-Type: text/plain\r\n\r\nhello"
    )
    last = b" world\r\n--boundary--\r\n"

    async def scenario():
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            if calls == 2 and outcome == "cancelled":
                raise asyncio.CancelledError
            return {
                "type": "http.request",
                "body": first if calls == 1 else last,
                "more_body": calls == 1,
            }

        request = Request(
            {
                "type": "http",
                "headers": [
                    (b"content-type", b"multipart/form-data; boundary=boundary"),
                    # 故意申报较小长度，实际限制必须按收到的字节计算。
                    (b"content-length", b"1"),
                ],
            },
            receive,
        )
        limiter = asyncio.Semaphore(1)

        async def upload():
            async with limited_upload_form(
                request,
                limiter,
                max_bytes=len(first) if outcome == "too_large" else 1024,
                max_fields=0,
                too_large_message="超过限制",
                invalid_form_message="表单无效",
            ) as form:
                assert limiter.locked()
                assert await form["file"].read() == b"hello world"
                if outcome == "business_error":
                    raise ValueError("保存失败")

        if outcome == "too_large":
            with pytest.raises(HTTPException) as caught:
                await upload()
            assert caught.value.status_code == 413
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await upload()
        elif outcome == "business_error":
            with pytest.raises(ValueError, match="保存失败"):
                await upload()
        else:
            await upload()
        assert not limiter.locked()
        assert files and all(file.closed for file in files)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,range_value,status,body",
    [
        ("GET", None, 200, b"0123456789"),
        ("GET", "bytes=2-5", 206, b"2345"),
        ("GET", "bytes=-3", 206, b"789"),
        ("HEAD", "bytes=2-5", 206, b""),
        ("GET", "bytes=99-100", 416, None),
    ],
)
def test_download_range_head_headers_and_connection_release(method, range_value, status, body):
    storage = Mock()
    storage.stat_object.return_value = ObjectInfo("private/file", 10, None, "text/html")
    opened = []

    @contextmanager
    def open_object(key, *, offset, length):
        assert key == "private/file"
        with BytesIO(b"0123456789"[offset : offset + length]) as stream:
            opened.append(stream)
            yield stream

    storage.open_object.side_effect = open_object
    app = FastAPI()

    @app.api_route("/file", methods=["GET", "HEAD"])
    def download(request: Request):
        return object_response(
            request,
            storage,
            key="private/file",
            filename="报告.txt",
            media_type="text/html",
            preview=True,
            missing_message="文件缺失",
            cache_control="private, no-store",
        )

    with TestClient(app) as client:
        response = client.request(
            method, "/file", headers={"Range": range_value} if range_value else {}
        )
    assert response.status_code == status
    if body is not None:
        assert response.content == body
        assert response.headers["cache-control"] == "private, no-store"
        assert "sandbox" in response.headers["content-security-policy"]
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
        assert response.headers["content-length"] == (
            "10" if range_value is None else "3" if range_value == "bytes=-3" else "4"
        )
    if method == "HEAD" or status == 416:
        storage.open_object.assert_not_called()
    else:
        assert opened and all(stream.closed for stream in opened)
    if status == 416:
        assert response.headers["content-range"] == "bytes */10"
