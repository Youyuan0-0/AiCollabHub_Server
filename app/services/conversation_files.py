"""会话附件复用文件表和对象存储；对象键固定记录暂存附件的会话归属。"""

from collections.abc import Iterator
from typing import BinaryIO

from fastapi import HTTPException
from sqlalchemy import select

from app.db.foundation import identifier
from app.schemas.conversations import ConversationAttachment
from app.services.attachment_store import ALLOWED_EXTENSIONS, MAX_ATTACHMENT_BYTES
from app.storage.files import FileStore


class ConversationFiles:
    """上传与读取均核对当前会话，发送消息时再原子绑定文件。"""

    def __init__(self, files: FileStore) -> None:
        self.files = files

    def _conversation(self, conversation_id: str) -> dict:
        table = self.files.foundation.table("conversations")
        with self.files.foundation.transaction(is_readonly=True) as session:
            row = (
                session.execute(
                    select(table).where(
                        table.c.id == identifier(conversation_id), table.c.deleted_at.is_(None)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "conversation_not_found")
            return dict(row)

    def save(self, conversation_id: str, name: str, stream: BinaryIO) -> ConversationAttachment:
        """仅向仍有效的会话上传文件；所属用户来自数据库，不接受前端指定。"""
        conversation = self._conversation(conversation_id)
        row = self.files.save(
            conversation["user_id"],
            f"conversations/{conversation['id']}",
            name,
            stream,
            maximum=MAX_ATTACHMENT_BYTES,
            extensions=ALLOWED_EXTENSIONS,
        )
        return self._view(row)

    def _row(self, conversation_id: str, file_id: str) -> dict:
        conversation = self._conversation(conversation_id)
        row = self.files.get(file_id)
        if row["uploader_id"] != conversation["user_id"] or not row["object_key"].startswith(
            f"conversations/{conversation['id']}/"
        ):
            raise HTTPException(404, "attachment_not_found")
        return row

    @staticmethod
    def _view(row: dict) -> ConversationAttachment:
        return ConversationAttachment(
            id=str(row["id"]), name=row["file_name"], size=row["size_bytes"]
        )

    def get(self, conversation_id: str, identifier: str) -> ConversationAttachment:
        """返回服务端确认的附件元信息。"""
        return self._view(self._row(conversation_id, identifier))

    def chunks(self, conversation_id: str, identifier: str) -> Iterator[bytes]:
        """重新核对会话归属后读取原始内容。"""
        yield from self.files.chunks(self._row(conversation_id, identifier))
