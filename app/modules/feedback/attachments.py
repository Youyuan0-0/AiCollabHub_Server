"""反馈附件的内容校验；存储与清理由统一文件服务负责。"""

import hashlib
from pathlib import PurePath
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException

from app.modules.feedback.schemas import FILE_TYPES, MAX_FILE_BYTES


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
