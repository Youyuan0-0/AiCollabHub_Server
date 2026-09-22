"""共用文件登记：对象存 MinIO，可信属性和生命周期存 files。"""

from collections.abc import Iterator
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, update

from app.db.foundation import FoundationDatabase, identifier, utc_now
from app.storage.client import MinioStorage


class FileStore:
    """登记上传前的占位记录；失败和未知提交保留待清理对象，不误删成功文件。"""

    def __init__(self, foundation: FoundationDatabase, storage: MinioStorage | None) -> None:
        self.foundation = foundation
        self.storage = storage

    def save(
        self,
        user_id: UUID,
        namespace: str,
        name: str,
        stream: BinaryIO,
        *,
        maximum: int,
        extensions: set[str],
        file_id: UUID | None = None,
        mime_type: str = "application/octet-stream",
    ) -> dict:
        """校验大小和名称后上传，编号可由业务幂等键派生，重试不得替换文件内容。"""
        if self.storage is None:
            raise HTTPException(503, "object_storage_not_configured")
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise HTTPException(422, "invalid_attachment_name")
        if Path(name).suffix.lower() not in extensions:
            raise HTTPException(415, "attachment_type_not_supported")
        key = file_id or uuid4()
        object_key = f"{namespace}/{key}/original"
        files = self.foundation.table("files")
        with SpooledTemporaryFile(max_size=1024 * 1024) as temporary:
            size, digest = 0, sha256()
            while chunk := stream.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(413, "attachment_too_large")
                digest.update(chunk)
                temporary.write(chunk)
            if not size:
                raise HTTPException(422, "attachment_empty")
            with self.foundation.transaction() as session:
                self.foundation.lock(session, f"file:{key}")
                old = session.execute(select(files).where(files.c.id == key)).mappings().first()
                if old:
                    if (
                        old["uploader_id"],
                        old["object_key"],
                        old["file_name"],
                        old["content_hash"],
                    ) != (user_id, object_key, name, digest.hexdigest()):
                        raise HTTPException(409, "upload_request_conflict")
                    if old["status"] == "ready":
                        return dict(old)
                    raise HTTPException(409, "upload_in_progress_or_expired")
                session.execute(
                    insert(files).values(
                        id=key,
                        uploader_id=user_id,
                        file_name=name,
                        mime_type=mime_type,
                        size_bytes=size,
                        content_hash=digest.hexdigest(),
                        bucket=self.storage.bucket,
                        object_key=object_key,
                        status="uploading",
                        expires_at=utc_now() + timedelta(days=1),
                    )
                )
            # 网络上传不占用数据库连接，失败保留带期限的占位供清理任务处理。
            temporary.seek(0)
            self.storage.put_object(object_key, temporary, length=size, content_type=mime_type)
            with self.foundation.transaction() as session:
                row = (
                    session.execute(
                        update(files)
                        .where(files.c.id == key, files.c.status == "uploading")
                        .values(status="ready", updated_at=utc_now())
                        .returning(files)
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise HTTPException(409, "upload_expired")
                return dict(row)

    def get(self, file_id: str | UUID) -> dict:
        """取得已就绪文件；调用方还须核对用户和业务归属。"""
        files = self.foundation.table("files")
        with self.foundation.transaction(is_readonly=True) as session:
            row = (
                session.execute(
                    select(files).where(
                        files.c.id == identifier(file_id),
                        files.c.status == "ready",
                        or_(files.c.expires_at.is_(None), files.c.expires_at > utc_now()),
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "attachment_not_found")
            return dict(row)

    def chunks(self, row: dict) -> Iterator[bytes]:
        """按已验证的内部对象键读取，流结束或中断均释放存储连接。"""
        if self.storage is None or row["bucket"] != self.storage.bucket:
            raise HTTPException(503, "object_storage_not_configured")
        with self.storage.open_object(row["object_key"]) as response:
            yield from response.stream(65536)
