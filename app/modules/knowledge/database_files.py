"""知识文档文件适配，原文及审核历史长期引用统一文件服务。"""

from typing import BinaryIO

from app.modules.common.identity import Actor
from app.modules.knowledge.documents import MAX_FILE_BYTES
from app.storage.files import FileStore


class KnowledgeDatabaseFiles:
    """文档 ID 与新上传文件 UUID 一致，数据库仍用 file_id 明确关联。"""

    def __init__(self, files: FileStore) -> None:
        self.files = files

    def save_document(self, stream: BinaryIO, name: str, actor: Actor) -> tuple[str, int]:
        """按实际上传用户登记文件，不使用本地目录保存正文。"""
        with self.files.foundation.transaction(is_readonly=True) as session:
            user_id = self.files.foundation.user_id(session, actor.id)
        row = self.files.save(
            user_id,
            "knowledge",
            name,
            stream,
            maximum=MAX_FILE_BYTES,
            extensions={".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv"},
        )
        return str(row["id"]), row["size_bytes"]

    def read(self, identifier: str) -> bytes:
        """仅供已在服务层校验知识库权限的解析与下载操作使用。"""
        return b"".join(self.files.chunks(self.files.get(identifier)))

    def delete(self, identifier: str) -> None:
        """保留审核历史引用的文件；未关联上传由 expires_at 清理任务处理。"""
