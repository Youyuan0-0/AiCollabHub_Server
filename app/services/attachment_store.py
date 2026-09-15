"""附件临时文件仓库；业务仅依赖协议，后续可替换为 PostgreSQL 元信息和对象存储。"""

import json
import re
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

from fastapi import HTTPException

from app.schemas.conversations import ConversationAttachment

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".txt",
    ".md",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".csv",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}


class AttachmentStore(Protocol):
    """conversation_id 限定文件归属；get 返回可信元信息，chunks 分块读取原始文件。"""

    def save(self, conversation_id: str, name: str, stream: BinaryIO) -> ConversationAttachment: ...
    def get(self, conversation_id: str, identifier: str) -> ConversationAttachment: ...
    def chunks(self, conversation_id: str, identifier: str) -> Iterator[bytes]: ...


class LocalAttachmentStore:
    """开发阶段共享目录适配器：落盘保留重启数据，不依赖临时会话表的具体结构。"""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, identifier: str, suffix: str) -> Path:
        # 客户端只能提交服务端生成的编号，不能把文件名或路径作为磁盘定位依据。
        if not re.fullmatch(r"attachment-[0-9a-f]{32}", identifier):
            raise HTTPException(404, "attachment_not_found")
        return self.root / f"{identifier}.{suffix}"

    def save(self, conversation_id: str, name: str, stream: BinaryIO) -> ConversationAttachment:
        """分块保存文件；超限或失败会清理本次临时文件，只有完整写入才发布元信息。"""
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
            raise HTTPException(422, "invalid_attachment_name")
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise HTTPException(415, "attachment_type_not_supported")
        identifier = f"attachment-{uuid4().hex}"
        data_path = self._path(identifier, "bin")
        metadata_path = self._path(identifier, "json")
        temporary = self._path(identifier, "tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            size = 0
            with data_path.open("xb") as output:
                while chunk := stream.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_ATTACHMENT_BYTES:
                        raise HTTPException(413, "attachment_too_large")
                    output.write(chunk)
            if size == 0:
                raise HTTPException(422, "attachment_empty")
            # 当前只保存原文件，不根据后缀宣称已解析或已通过内容检测；下载一律强制附件。
            attachment = ConversationAttachment(id=identifier, name=name, size=size)
            temporary.write_text(
                json.dumps(
                    {"conversationId": conversation_id, "attachment": attachment.model_dump()}
                ),
                encoding="utf-8",
            )
            temporary.replace(metadata_path)
            return attachment
        except Exception as error:
            for path in (temporary, data_path):
                # 清理失败不能遮蔽原始业务错误，磁盘不可用统一返回可重试状态。
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            if isinstance(error, OSError):
                raise HTTPException(503, "attachment_storage_unavailable") from None
            raise

    def get(self, conversation_id: str, identifier: str) -> ConversationAttachment:
        """读取并校验附件所属会话；损坏记录报告不可用，未知或跨会话编号均返回 404。"""
        path = self._path(identifier, "json")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["conversationId"] != conversation_id:
                raise HTTPException(404, "attachment_not_found")
            attachment = ConversationAttachment.model_validate(record["attachment"])
            if (
                attachment.id != identifier
                or self._path(identifier, "bin").stat().st_size != attachment.size
            ):
                raise HTTPException(503, "attachment_storage_unavailable")
            return attachment
        except FileNotFoundError:
            raise HTTPException(404, "attachment_not_found") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "attachment_storage_unavailable") from None

    def chunks(self, conversation_id: str, identifier: str) -> Iterator[bytes]:
        """下载前重新确认归属；生成器退出时关闭文件，避免大附件常驻内存。"""
        self.get(conversation_id, identifier)
        with self._path(identifier, "bin").open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                yield chunk
