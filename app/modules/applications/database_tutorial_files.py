"""教程文件不可变元信息与场景数据库字段之间的映射。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import insert

from app.modules.applications.database_values import date_value, iso

if TYPE_CHECKING:
    from app.modules.applications.database_session import DatabaseApplicationSession

FILE_COLUMNS = {
    "id": "id",
    "createdBy": "created_by_subject",
    "organizationId": "organization_id",
    "name": "original_name",
    "size": "byte_size",
    "contentType": "content_type",
    "kind": "kind",
    "securityLevel": "security_level",
    "objectKey": "object_key",
    "previewKey": "preview_key",
    "previewContentType": "preview_content_type",
}


def read_file(session: DatabaseApplicationSession, key: str) -> dict | None:
    """按编号读取附件，不存在时返回空值。"""
    table = session.table("tutorial_file")
    row = session.one("tutorial_file", table.c.id == key)
    if row is None:
        return None
    return {
        **{field: row[column] for field, column in FILE_COLUMNS.items()},
        "createdAt": iso(row["created_at"]),
    }


def insert_file(session: DatabaseApplicationSession, value: dict) -> None:
    """写入新附件；原文件和元信息不允许被后续上传覆盖。"""
    session.write(
        insert(session.table("tutorial_file")).values(
            **{column: value[field] for field, column in FILE_COLUMNS.items()},
            created_at=date_value(value["createdAt"]),
        )
    )
