"""知识原文的受控本地存储；数据库模式的多个实例须挂载同一持久卷。"""

import re
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from fastapi import HTTPException

from app.modules.knowledge.documents import MAX_FILE_BYTES


class KnowledgeFiles:
    """以随机编号保存原文，支持索引失败后的重新解析及进程重启后重建。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise HTTPException(404, "知识文档不存在")
        return self.root / identifier

    def save(self, stream: BinaryIO) -> tuple[str, int]:
        """流式保存最多 100MB 的非空文档，异常时清理仅本次创建的文件。"""
        identifier = uuid4().hex
        path = self._path(identifier)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            size = 0
            with path.open("xb") as output:
                while chunk := stream.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, "单个文档不能超过 100MB")
                    output.write(chunk)
            if not size:
                raise HTTPException(422, "不能上传空文档")
            return identifier, size
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def read(self, identifier: str) -> bytes:
        """读取服务端生成的存储编号；原文缺失时不回退为虚构内容。"""
        try:
            return self._path(identifier).read_bytes()
        except OSError:
            raise HTTPException(503, "知识文档原文件不可用，请联系管理员") from None

    def delete(self, identifier: str) -> None:
        """仅清理已经解除业务引用的文件或本次未提交成功的文件。"""
        self._path(identifier).unlink(missing_ok=True)
