"""问题反馈四表适配，保留原前端契约并复用统一文件服务。"""

from typing import BinaryIO
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.db.foundation import FoundationDatabase, identifier, iso, utc_now
from app.modules.feedback.schemas import (
    FEEDBACK_MODULES,
    FEEDBACK_TYPES,
    FILE_TYPES,
    MAX_FILE_BYTES,
    FeedbackInput,
    FeedbackQuery,
    FeedbackUpdate,
)
from app.storage.files import FileStore


class FeedbackService:
    """反馈正文不可改，处理事件追加保存，文件本体与反馈关联分开管理。"""

    def __init__(
        self,
        foundation: FoundationDatabase,
        files: FileStore,
        subject: str,
        *,
        is_admin: bool = False,
    ) -> None:
        self.foundation, self.files, self.subject, self.is_admin = (
            foundation,
            files,
            subject,
            is_admin,
        )
        self.records = foundation.table("feedback_records")
        self.events = foundation.table("feedback_events")
        self.links = foundation.table("feedback_attachments")
        self.file_table = foundation.table("files")
        self.users = foundation.table("users")

    def _owned(self, session: Session, record_id: str, *, lock: bool = False) -> dict:
        query = select(self.records).where(self.records.c.id == identifier(record_id))
        if not self.is_admin:
            query = query.where(
                self.records.c.submitter_id == self.foundation.user_id(session, self.subject)
            )
        row = session.execute(query.with_for_update() if lock else query).mappings().first()
        if row is None:
            raise HTTPException(404, "反馈不存在或无权查看")
        return dict(row)

    def _summary(self, session: Session, row: dict, latest: dict | None = None) -> dict:
        owner = self.foundation.user(session, row["submitter_id"])
        return {
            "id": str(row["id"]),
            "ownerName": owner["name"],
            "type": row["feedback_type"],
            "typeLabel": FEEDBACK_TYPES[row["feedback_type"]],
            "typeOther": row["type_other"],
            "module": row["module_code"],
            "moduleLabel": FEEDBACK_MODULES[row["module_code"]],
            "moduleOther": row["module_other"],
            "title": row["title"],
            "description": row["description"],
            "contact": row["contact_info"],
            "status": row["status"],
            "version": row["revision"],
            "createdAt": iso(row["created_at"]),
            "updatedAt": iso(row["updated_at"]),
            "reply": latest["content"] if latest else "",
        }

    @staticmethod
    def file_view(file: dict, *, attached: bool = False) -> dict:
        """保持附件编号为 files.id，不暴露存储桶和内部对象键。"""
        return {
            "id": str(file["id"]),
            "name": file["file_name"],
            "size": file["size_bytes"] or 0,
            "contentType": file["mime_type"],
            "state": "attached"
            if attached
            else {"ready": "staged", "failed": "deleting", "deleted": "deleting"}.get(
                file["status"], file["status"]
            ),
            "securityLevel": None,
            "expiresAt": iso(file["expires_at"]),
        }

    def _detail(self, session: Session, row: dict, before: int | None = None) -> dict:
        query = (
            select(self.events, self.users.c.display_name, self.users.c.iam_subject)
            .join(self.users, self.users.c.id == self.events.c.actor_id)
            .where(self.events.c.feedback_id == row["id"])
        )
        latest = (
            session.execute(query.order_by(self.events.c.resulting_revision.desc()).limit(1))
            .mappings()
            .first()
        )
        if before:
            query = query.where(self.events.c.resulting_revision < before)
        events = list(
            session.execute(
                query.order_by(self.events.c.resulting_revision.desc()).limit(21)
            ).mappings()
        )
        attached = session.execute(
            select(self.file_table)
            .join(self.links, self.links.c.file_id == self.file_table.c.id)
            .where(self.links.c.feedback_id == row["id"])
            .order_by(self.links.c.position)
        ).mappings()
        return {
            **self._summary(session, row, latest),
            "attachments": [self.file_view(dict(file), attached=True) for file in attached],
            "events": [
                {
                    "id": str(item["id"]),
                    "action": item["action"],
                    "actorName": item["display_name"] or item["iam_subject"],
                    "content": item["content"],
                    "fromStatus": item["from_status"],
                    "toStatus": item["to_status"],
                    "version": item["resulting_revision"],
                    "createdAt": iso(item["created_at"]),
                }
                for item in events[:20]
            ],
            "nextEventVersion": events[19]["resulting_revision"] if len(events) > 20 else None,
        }

    def detail(self, record_id: str, before: int | None = None) -> dict:
        """按权限读取反馈及分页处理历史。"""
        with self.foundation.transaction(is_readonly=True) as session:
            return self._detail(session, self._owned(session, record_id), before)

    def page(self, query: FeedbackQuery) -> dict:
        """使用数据库分页，按用户隔离列表。"""
        with self.foundation.transaction(is_readonly=True) as session:
            conditions = (
                []
                if self.is_admin
                else [self.records.c.submitter_id == self.foundation.user_id(session, self.subject)]
            )
            if query.status != "all":
                conditions.append(
                    self.records.c.status
                    == ("processing" if query.status == "open" else query.status)
                )
            if query.q.strip():
                conditions.append(self.records.c.title.contains(query.q.strip(), autoescape=True))
            total = session.scalar(
                select(func.count()).select_from(self.records).where(*conditions)
            )
            rows = (
                session.execute(
                    select(self.records)
                    .where(*conditions)
                    .order_by(self.records.c.created_at.desc(), self.records.c.id)
                    .offset((query.page - 1) * query.page_size)
                    .limit(query.page_size)
                )
                .mappings()
                .all()
            )
            latest = {
                item["feedback_id"]: item
                for item in session.execute(
                    select(self.events)
                    .join(
                        self.records,
                        (self.records.c.id == self.events.c.feedback_id)
                        & (self.records.c.revision == self.events.c.resulting_revision),
                    )
                    .where(self.records.c.id.in_([row["id"] for row in rows]))
                ).mappings()
            }
            return {
                "items": [self._summary(session, dict(row), latest.get(row["id"])) for row in rows],
                "total": total,
                "page": query.page,
                "pageSize": query.page_size,
            }

    def submission(self, request_id: str) -> dict:
        """按提交幂等键核对网络断开后的真实结果。"""
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, self.subject)
            row = (
                session.execute(
                    select(self.records).where(
                        self.records.c.submitter_id == user_id,
                        self.records.c.idempotency_key == identifier(request_id),
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "尚未查到该提交记录")
            return self._detail(session, dict(row))

    def submit(self, data: FeedbackInput) -> tuple[dict, bool]:
        """原始资料、附件引用和首次事件同事务保存，重复请求比较真实内容。"""
        with self.foundation.transaction() as session:
            user_id = self.foundation.user_id(session, self.subject)
            self.foundation.lock(session, f"feedback:{user_id}:{data.client_request_id}")
            values = dict(
                feedback_type=data.type,
                type_other=data.type_other,
                module_code=data.module,
                module_other=data.module_other,
                title=data.title,
                description=data.description,
                contact_info=data.contact,
            )
            old = (
                session.execute(
                    select(self.records).where(
                        self.records.c.submitter_id == user_id,
                        self.records.c.idempotency_key == data.client_request_id,
                    )
                )
                .mappings()
                .first()
            )
            if old:
                attached = list(
                    session.scalars(
                        select(self.links.c.file_id)
                        .where(self.links.c.feedback_id == old["id"])
                        .order_by(self.links.c.position)
                    )
                )
                if (
                    any(old[key] != value for key, value in values.items())
                    or attached != data.attachment_ids
                ):
                    raise HTTPException(409, "同一提交编号不能替换反馈内容")
                return self._detail(session, dict(old)), False
            key = uuid4()
            row = (
                session.execute(
                    insert(self.records)
                    .values(
                        id=key,
                        submitter_id=user_id,
                        idempotency_key=data.client_request_id,
                        **values,
                    )
                    .returning(self.records)
                )
                .mappings()
                .one()
            )
            for position, file_id in enumerate(data.attachment_ids, 1):
                file = (
                    session.execute(
                        select(self.file_table)
                        .where(
                            self.file_table.c.id == file_id,
                            self.file_table.c.uploader_id == user_id,
                            self.file_table.c.status == "ready",
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                used = session.scalar(
                    select(self.links.c.id).where(self.links.c.file_id == file_id)
                )
                if (
                    file is None
                    or not file["object_key"].startswith("feedback/")
                    or used is not None
                ):
                    raise HTTPException(409, "附件不可用或已关联其他反馈")
                session.execute(
                    insert(self.links).values(
                        id=uuid4(), feedback_id=key, file_id=file_id, position=position
                    )
                )
            session.execute(
                insert(self.events).values(
                    id=uuid4(),
                    feedback_id=key,
                    actor_id=user_id,
                    action="submitted",
                    content="",
                    to_status="processing",
                    resulting_revision=1,
                    idempotency_key=data.client_request_id,
                )
            )
            return self._detail(session, dict(row)), True

    def process(self, record_id: str, data: FeedbackUpdate) -> dict:
        """管理员处理使用行锁和修订校验；重试返回已提交结果而不重复追加事件。"""
        if not self.is_admin:
            raise HTTPException(403, "没有处理反馈的权限")
        with self.foundation.transaction() as session:
            row = self._owned(session, record_id, lock=True)
            user_id = self.foundation.user_id(session, self.subject)
            old = (
                session.execute(
                    select(self.events).where(
                        self.events.c.feedback_id == row["id"],
                        self.events.c.actor_id == user_id,
                        self.events.c.idempotency_key == data.client_request_id,
                    )
                )
                .mappings()
                .first()
            )
            if old:
                if (old["content"], old["to_status"], old["resulting_revision"]) != (
                    data.reply,
                    data.status,
                    data.expected_version + 1,
                ):
                    raise HTTPException(409, "同一处理编号不能替换内容")
                return self._detail(session, row)
            if row["revision"] != data.expected_version:
                raise HTTPException(409, "反馈已被处理，请刷新后重试")
            action = (
                "replied"
                if row["status"] == data.status
                else "resolved"
                if data.status == "resolved"
                else "reopened"
            )
            updated = (
                session.execute(
                    update(self.records)
                    .where(self.records.c.id == row["id"])
                    .values(
                        status=data.status,
                        revision=row["revision"] + 1,
                        resolved_at=(row["resolved_at"] or utc_now())
                        if data.status == "resolved"
                        else None,
                    )
                    .returning(self.records)
                )
                .mappings()
                .one()
            )
            session.execute(
                insert(self.events).values(
                    id=uuid4(),
                    feedback_id=row["id"],
                    actor_id=user_id,
                    action=action,
                    content=data.reply,
                    from_status=row["status"],
                    to_status=data.status,
                    resulting_revision=updated["revision"],
                    idempotency_key=data.client_request_id,
                )
            )
            return self._detail(session, dict(updated))

    def overview(self) -> dict:
        """统计反馈处理状态，空库返回零。"""
        if not self.is_admin:
            raise HTTPException(403, "没有管理权限")
        with self.foundation.transaction(is_readonly=True) as session:
            counts = dict(
                session.execute(
                    select(self.records.c.status, func.count()).group_by(self.records.c.status)
                ).all()
            )
            return {
                "total": sum(counts.values()),
                "open": counts.get("processing", 0),
                "resolved": counts.get("resolved", 0),
            }

    def _upload_id(self, request_id: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"aicollabhub:feedback:{self.subject}:{identifier(request_id)}")

    def save_upload(self, request_id: str, name: str, stream: BinaryIO, level: str | None) -> dict:
        """原请求号生成固定 UUID，重试上传会核对文件摘要。"""
        from app.modules.feedback.attachments import inspect_file

        if level:
            raise HTTPException(422, "平台密级字典尚未配置")
        mime, _, _ = inspect_file(stream, name)
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, self.subject)
        self.files.save(
            user_id,
            "feedback",
            name,
            stream,
            maximum=MAX_FILE_BYTES,
            extensions=set(FILE_TYPES),
            file_id=self._upload_id(request_id),
            mime_type=mime,
        )
        return self.upload_status(request_id)

    def upload_status(self, request_id: str) -> dict:
        """返回当前用户的暂存上传状态，包括尚未完成的上传。"""
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, self.subject)
            row = (
                session.execute(
                    select(self.file_table).where(
                        self.file_table.c.id == self._upload_id(request_id),
                        self.file_table.c.uploader_id == user_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HTTPException(404, "尚未查到该上传记录")
            attached = (
                session.scalar(select(self.links.c.id).where(self.links.c.file_id == row["id"]))
                is not None
            )
            return self.file_view(dict(row), attached=attached)

    def remove_upload(self, file_id: str) -> None:
        """只取消自己未关联的暂存文件，清理器负责重试对象删除。"""
        with self.foundation.transaction() as session:
            user_id = self.foundation.user_id(session, self.subject)
            file = (
                session.execute(
                    select(self.file_table)
                    .where(
                        self.file_table.c.id == identifier(file_id),
                        self.file_table.c.uploader_id == user_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if file is None or file["status"] in {"deleting", "deleted"}:
                return
            if not file["object_key"].startswith("feedback/") or session.scalar(
                select(self.links.c.id).where(self.links.c.file_id == file["id"])
            ):
                raise HTTPException(409, "已提交反馈的附件不能删除")
            session.execute(
                update(self.file_table)
                .where(self.file_table.c.id == file["id"])
                .values(status="deleting", expires_at=utc_now())
            )

    def download(self, record_id: str, file_id: str) -> StreamingResponse:
        """校验反馈权限和文件关联后流式下载原始文件。"""
        with self.foundation.transaction(is_readonly=True) as session:
            row = self._owned(session, record_id)
            file = (
                session.execute(
                    select(self.file_table)
                    .join(self.links, self.links.c.file_id == self.file_table.c.id)
                    .where(
                        self.links.c.feedback_id == row["id"],
                        self.file_table.c.id == identifier(file_id),
                        self.file_table.c.status == "ready",
                    )
                )
                .mappings()
                .first()
            )
            if file is None:
                raise HTTPException(404, "附件不存在")
            value = dict(file)
        return StreamingResponse(
            self.files.chunks(value),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(value["size_bytes"]),
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(value["file_name"], safe=""),
                "Cache-Control": "private, no-store",
            },
        )
