"""反馈文件的内容校验、MinIO 暂存、下载和到期清理。"""

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from pathlib import PurePath
from typing import BinaryIO
from urllib.parse import quote
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.feedback.models import FeedbackAttachment
from app.modules.feedback.schemas import (
    FILE_TYPES,
    MAX_FILE_BYTES,
    OBJECT_PREFIX,
    TEMPORARY_SECONDS,
)
from app.modules.feedback.service import FeedbackActor, attachment_value, aware, utc_now
from app.storage.client import MinioStorage, ObjectNotFound, StorageUnavailable


def inspect_file(stream: BinaryIO, name: str) -> tuple[str, int, str]:
    """按实际字节数、文件特征和有界 DOCX 目录检查核验上传资料。"""
    if not name or len(name) > 200 or any(ord(c) < 32 for c in name) or "/" in name or "\\" in name:
        raise HTTPException(422, "附件名称无效，请重命名后上传")
    extension = PurePath(name).suffix.lower()
    if extension not in FILE_TYPES:
        raise HTTPException(415, "支持 PNG、JPG、JPEG、PDF 和 DOCX 附件")
    stream.seek(0)
    header = stream.read(1024)
    valid = (
        (extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension in (".jpg", ".jpeg") and header.startswith(b"\xff\xd8\xff"))
        or (extension == ".pdf" and header.startswith(b"%PDF-"))
    )
    if extension == ".docx":
        # 先检查 ZIP 尾部声明的目录大小，避免 ZipFile 读取恶意超大中心目录。
        stream.seek(0, 2)
        length = stream.tell()
        stream.seek(max(0, length - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset >= 0 and len(tail) >= offset + 22:
            count = int.from_bytes(tail[offset + 10 : offset + 12], "little")
            directory_size = int.from_bytes(tail[offset + 12 : offset + 16], "little")
            if count < 10000 and directory_size <= 4 * 1024 * 1024:
                try:
                    with ZipFile(stream) as document:
                        names = set(document.namelist())
                        valid = {"[Content_Types].xml", "word/document.xml"} <= names
                except (BadZipFile, OSError, ValueError):
                    valid = False
    if not valid:
        raise HTTPException(415, "附件内容与文件类型不一致，或文档格式不受支持")
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while block := stream.read(1024 * 1024):
        size += len(block)
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, "单个附件不能超过 1 GiB（1024 MiB）")
        digest.update(block)
    if not size:
        raise HTTPException(422, "不能上传空文件")
    stream.seek(0)
    return FILE_TYPES[extension], size, digest.hexdigest()


def find_upload(db: Session, owner: FeedbackActor, request_id: str) -> FeedbackAttachment | None:
    """核对自己的上传编号，防止读取其他用户的暂存资料。"""
    return db.scalar(
        select(FeedbackAttachment).where(
            FeedbackAttachment.uploader_id == owner.id,
            FeedbackAttachment.client_request_id == request_id,
        )
    )


def save_upload(
    db: Session,
    settings: Settings,
    storage: MinioStorage,
    owner: FeedbackActor,
    request_id: str,
    name: str,
    stream: BinaryIO,
    security_level: str | None,
) -> dict:
    """保存单个文件，提交结果不确定时保留元信息供原编号核对。"""
    if security_level not in (None, ""):
        raise HTTPException(422, "平台密级字典尚未配置，不能提交自定义密级")
    content_type, size, sha256 = inspect_file(stream, name)

    def repeat(item: FeedbackAttachment) -> dict:
        if item.original_name != name or item.sha256 != sha256 or item.byte_size != size:
            raise HTTPException(409, "同一上传编号不能替换成其他文件")
        if item.state in ("staged", "attached") and (
            not item.expires_at or aware(item.expires_at) > utc_now()
        ):
            return attachment_value(item)
        raise HTTPException(409, "上传尚未完成或附件已过期，请核对上传状态后重试")

    existing = find_upload(db, owner, request_id)
    if existing:
        return repeat(existing)
    now = utc_now()
    attachment_id = str(uuid4())
    item = FeedbackAttachment(
        id=attachment_id,
        uploader_id=owner.id,
        client_request_id=request_id,
        original_name=name,
        content_type=content_type,
        byte_size=size,
        sha256=sha256,
        storage_backend="minio",
        storage_bucket=storage.bucket,
        object_key=OBJECT_PREFIX + attachment_id,
        state="uploading",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=TEMPORARY_SECONDS),
    )
    prepare_write(db, settings)
    try:
        db.add(item)
        commit_write(db)
    except IntegrityError:
        db.rollback()
        existing = find_upload(db, owner, request_id)
        if existing:
            return repeat(existing)
        raise HTTPException(409, "上传编号冲突，请重试") from None
    # 存储与数据库没有分布式事务，先保留唯一上传记录，再写入固定对象键。
    try:
        storage.put_object(item.object_key, stream, length=size, content_type=content_type)
    except (StorageUnavailable, ValueError):
        prepare_write(db, settings)
        db.execute(
            update(FeedbackAttachment)
            .where(FeedbackAttachment.id == item.id, FeedbackAttachment.state == "uploading")
            .values(state="deleting", updated_at=utc_now(), expires_at=utc_now())
        )
        commit_write(db)
        raise HTTPException(503, "文件保存失败，请重试该附件") from None
    prepare_write(db, settings)
    changed = db.execute(
        update(FeedbackAttachment)
        .where(
            FeedbackAttachment.id == item.id,
            FeedbackAttachment.state == "uploading",
            FeedbackAttachment.expires_at > utc_now(),
        )
        .values(state="staged", updated_at=utc_now())
    )
    if changed.rowcount != 1:
        db.rollback()
        storage.remove_object(item.object_key)
        raise HTTPException(409, "上传已取消或过期，请重新上传")
    # 提交断连时不删除对象：可能已经持久化成功，客户端使用原编号核对。
    commit_write(db)
    db.refresh(item)
    return attachment_value(item)


def remove_temporary(
    db: Session, settings: Settings, storage: MinioStorage, owner: FeedbackActor, attachment_id: str
) -> None:
    """取消自己的临时附件，保留清理标记处理迟到的上传和存储错误。"""
    item = db.scalar(
        select(FeedbackAttachment).where(
            FeedbackAttachment.id == attachment_id, FeedbackAttachment.uploader_id == owner.id
        )
    )
    if item is None:
        return
    prepare_write(db, settings)
    result = db.execute(
        update(FeedbackAttachment)
        .where(
            FeedbackAttachment.id == attachment_id,
            FeedbackAttachment.feedback_id.is_(None),
            FeedbackAttachment.state.in_(["uploading", "staged", "deleting"]),
        )
        .values(
            state="deleting",
            updated_at=utc_now(),
            expires_at=utc_now() + timedelta(seconds=TEMPORARY_SECONDS),
        )
    )
    if result.rowcount != 1:
        raise HTTPException(409, "已提交反馈的附件不能从暂存区删除")
    commit_write(db)
    if item.storage_bucket != storage.bucket:
        raise HTTPException(503, "附件存储桶配置已变更，请联系管理员")
    try:
        storage.remove_object(item.object_key)
    except StorageUnavailable:
        # 删除意图已落库，后续清理器会继续尝试，不阻止用户移除该附件。
        pass


def download(
    db: Session, storage: MinioStorage, record_id: str, attachment_id: str
) -> StreamingResponse:
    """在路由已校验反馈归属后，按块输出附件并始终释放存储连接。"""
    item = db.scalar(
        select(FeedbackAttachment).where(
            FeedbackAttachment.id == attachment_id,
            FeedbackAttachment.feedback_id == record_id,
            FeedbackAttachment.state == "attached",
        )
    )
    if item is None:
        raise HTTPException(404, "附件不存在")
    if item.storage_bucket != storage.bucket:
        raise HTTPException(503, "附件存储桶配置已变更，请联系管理员")
    if item.security_level:
        raise HTTPException(403, "该附件需要已配置的密级授权策略")
    info = storage.stat_object(item.object_key)
    if info.size != item.byte_size:
        raise HTTPException(503, "附件内容异常，请联系管理员")

    def blocks() -> Iterator[bytes]:
        with storage.open_object(item.object_key) as response:
            yield from response.stream(1024 * 1024)

    return StreamingResponse(
        blocks(),
        media_type=item.content_type,
        headers={
            "Content-Length": str(item.byte_size),
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(item.original_name, safe=""),
            "Cache-Control": "private, no-store",
        },
    )


def cleanup(db: Session, settings: Settings, storage: MinioStorage, limit: int = 100) -> dict:
    """逐批清理到期临时附件；绑定条件和领取锁防止误删及多进程争用。"""
    result = {"cleaned": 0, "deferred": 0}
    for _ in range(limit):
        now = utc_now()
        prepare_write(db, settings)
        item = db.scalar(
            select(FeedbackAttachment)
            .where(
                FeedbackAttachment.feedback_id.is_(None),
                FeedbackAttachment.expires_at <= now,
                FeedbackAttachment.state.in_(["uploading", "staged", "deleting"]),
                FeedbackAttachment.storage_bucket == storage.bucket,
            )
            .order_by(FeedbackAttachment.expires_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if item is None:
            db.rollback()
            break
        was_deleting = item.state == "deleting"
        item.state = "deleting"
        item.updated_at = now
        # 上传中被领取时保留一天墓碑，再次删除对象后才移除元信息。
        item.expires_at = now + timedelta(seconds=TEMPORARY_SECONDS)
        commit_write(db)
        try:
            storage.remove_object(item.object_key)
        except (StorageUnavailable, ObjectNotFound):
            result["deferred"] += 1
            continue
        if was_deleting:
            db.delete(item)
            commit_write(db)
        result["cleaned"] += 1
    return result
