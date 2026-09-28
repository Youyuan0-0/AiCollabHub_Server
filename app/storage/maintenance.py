"""领取无业务引用的过期文件，数据库确认删除资格后再回收对象。"""

from datetime import timedelta

from sqlalchemy import and_, delete, or_, select, update

from app.db.foundation import FoundationDatabase, utc_now
from app.storage.client import MinioStorage

REFERENCE_TABLES = (
    "feedback_attachments",
    "resource_downloads",
    "resource_file_references",
    "conversation_message_attachments",
    "knowledge_documents",
)


def cleanup_files(
    foundation: FoundationDatabase, storage: MinioStorage, *, limit: int = 100
) -> dict[str, int]:
    """分批领取无引用的过期文件，在事务外删除对象，再记录删除结果。

    limit 限制候选原件数，返回的 deleted/deferred 按文件数计，包含关联预览。
    首次扫描只选候选；真正删除前锁定原件和预览，并重新核对引用和期限，防止
    扫描之后已被业务使用的文件遭到回收。deleting 状态保留五分钟租约以便失败续作。
    """
    files, previews = foundation.table("files"), foundation.table("file_previews")

    def unused(file_id: object) -> object:
        return and_(
            *(
                ~select(table.c.id).where(table.c.file_id == file_id).exists()
                for table in (foundation.table(name) for name in REFERENCE_TABLES)
            )
        )

    now = utc_now()
    with foundation.transaction() as session:
        candidates = (
            session.execute(
                select(files.c.id)
                .where(
                    files.c.status != "deleted",
                    files.c.bucket == storage.bucket,
                    or_(
                        files.c.expires_at <= now,
                        and_(
                            files.c.expires_at.is_(None),
                            files.c.updated_at <= now - timedelta(days=1),
                        ),
                    ),
                    unused(files.c.id),
                    ~select(previews.c.id).where(previews.c.preview_file_id == files.c.id).exists(),
                )
                .order_by(files.c.updated_at, files.c.id)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    deleted, deferred = 0, 0
    for candidate in candidates:
        with foundation.transaction() as session:
            preview_ids = session.scalars(
                select(previews.c.preview_file_id).where(previews.c.source_file_id == candidate)
            ).all()
            rows = (
                session.execute(
                    select(files)
                    .where(files.c.id.in_([candidate, *preview_ids]))
                    .order_by(files.c.id)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            # 任一关联文件已被其他 worker 锁定就跳过整组，不能只删除拿到锁的部分文件。
            if len(rows) != len(preview_ids) + 1 or any(
                row["status"] == "deleted" or row["bucket"] != storage.bucket for row in rows
            ):
                continue
            if any(not session.scalar(select(unused(row["id"]))) for row in rows):
                continue
            source = next(row for row in rows if row["id"] == candidate)
            if source["expires_at"] and source["expires_at"] > now:
                continue
            session.execute(delete(previews).where(previews.c.source_file_id == candidate))
            ids = [row["id"] for row in rows]
            session.execute(
                update(files)
                .where(files.c.id.in_(ids))
                .values(status="deleting", expires_at=now + timedelta(minutes=5))
            )
        # 对象存储请求发生在事务之外；失败保留 deleting 状态供后续批次续作。
        try:
            for row in rows:
                storage.remove_object(row["object_key"])
        except Exception:
            deferred += len(rows)
            continue
        with foundation.transaction() as session:
            session.execute(
                update(files)
                .where(files.c.id.in_(ids), files.c.status == "deleting")
                .values(status="deleted", expires_at=None)
            )
        deleted += len(rows)
    return {"deleted": deleted, "deferred": deferred}
