"""审核报告文件元信息的数据库映射，审核关联仅能由空值写入一次。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import delete, insert, update

from app.modules.applications.database_values import date_value, iso

if TYPE_CHECKING:
    from app.modules.applications.database_session import DatabaseApplicationSession

FILE_COLUMNS = {
    "id": "id",
    "applicationId": "application_id",
    "publicationId": "publication_id",
    "publicationRevision": "publication_revision",
    "reviewId": "review_note_id",
    "createdBy": "created_by_subject",
    "name": "original_name",
    "size": "byte_size",
    "contentType": "content_type",
    "bucket": "bucket_name",
    "objectKey": "object_key",
    "previewKey": "preview_key",
    "previewContentType": "preview_content_type",
}


def file_view(row: dict) -> dict:
    """将可信文件索引转换为内部业务结构。"""
    return {
        **{field: row[column] for field, column in FILE_COLUMNS.items()},
        "createdAt": iso(row["created_at"]),
    }


def read_file(session: DatabaseApplicationSession, key: str) -> dict | None:
    """读取单个报告索引。"""
    row = session.one("review_report_file", session.table("review_report_file").c.id == key)
    return file_view(row) if row else None


def save_file(session: DatabaseApplicationSession, value: dict) -> None:
    """登记新报告或原子绑定审核记录，不允许重写历史文件。"""
    table = session.table("review_report_file")
    existing = read_file(session, value["id"])
    if existing is None:
        session.write(
            insert(table).values(
                **{column: value[field] for field, column in FILE_COLUMNS.items()},
                created_at=date_value(value["createdAt"]),
            )
        )
    else:
        if existing["reviewId"] is not None or not value["reviewId"]:
            raise HTTPException(409, "已提交的报告不能修改")
        session.write(
            update(table)
            .where(table.c.id == value["id"], table.c.review_note_id.is_(None))
            .values(review_note_id=value["reviewId"])
        )


def remove_file(session: DatabaseApplicationSession, key: str) -> None:
    """只移除尚未提交的报告索引；历史报告不能删除。"""
    table = session.table("review_report_file")
    result = session.write(delete(table).where(table.c.id == key, table.c.review_note_id.is_(None)))
    if result.rowcount != 1:
        raise HTTPException(409, "报告已提交或已移除，请刷新后重试")
