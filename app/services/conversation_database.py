"""新问答表的事务仓储：先持久化提问，再在事务外调用模型，最后保存结果。"""

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.db.foundation import FoundationDatabase, identifier, iso, utc_now
from app.schemas.conversations import (
    ChatInput,
    ChatReply,
    ConversationAttachment,
    ConversationMessage,
    ConversationRecord,
    ConversationSummary,
    NewConversationInput,
)


class ConversationDatabase:
    """所有查询绑定可信用户，消息顺序与重复提交由同一会话行锁保护。"""

    def __init__(self, foundation: FoundationDatabase, subject: str) -> None:
        self.foundation = foundation
        self.subject = subject
        self.conversations = foundation.table("conversations")
        self.messages = foundation.table("conversation_messages")
        self.attachments = foundation.table("conversation_message_attachments")
        self.files = foundation.table("files")

    def _owned(self, session: Session, conversation_id: str, *, lock: bool = False) -> dict:
        """把身份归属和软删除条件放入查询；写流程可同时锁住会话行。

        查询不到与无权读取统一返回 404，避免通过会话编号探知他人记录。
        会话行锁负责协调消息序号、发送幂等检查、回答完成和删除操作。
        """
        user_id = self.foundation.user_id(session, self.subject)
        query = select(self.conversations).where(
            self.conversations.c.id == identifier(conversation_id),
            self.conversations.c.user_id == user_id,
            self.conversations.c.deleted_at.is_(None),
        )
        if lock:
            query = query.with_for_update()
        row = session.execute(query).mappings().first()
        if row is None:
            raise HTTPException(404, "conversation_not_found")
        return dict(row)

    def _choice(self, session: Session, table_name: str, value: str, user_id: UUID) -> tuple:
        """名称兼容现有模型选择器；正式资源编号必须可见且已启用。"""
        table = self.foundation.table(table_name)
        try:
            key = UUID(value)
        except (ValueError, TypeError):
            key = None
        row = (
            session.execute(
                select(table).where(
                    table.c.id == key if key else table.c.name == value,
                    table.c.status == "enabled",
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            if key:
                raise HTTPException(404, "conversation_resource_unavailable")
            # 供应方仍为 Mock，默认路由名可不对应本地正式资源。
            return None, value
        scopes = self.foundation.table("resource_visibility_policies")
        targets = self.foundation.table("resource_visibility_targets")
        kind = "model" if table_name == "models" else "knowledge"
        scope = (
            session.execute(
                select(scopes).where(
                    scopes.c.resource_type == kind, scopes.c.resource_id == row["id"]
                )
            )
            .mappings()
            .first()
        )
        if (
            scope
            and scope["scope"] == "members"
            and session.scalar(
                select(targets.c.id).where(
                    targets.c.policy_id == scope["id"], targets.c.user_id == user_id
                )
            )
            is None
        ):
            raise HTTPException(404, "conversation_resource_unavailable")
        return row["id"], row["name"]

    def _context(
        self, session: Session, data: ChatInput | NewConversationInput, user_id: UUID
    ) -> dict:
        model_id, model_name = self._choice(session, "models", data.model, user_id)
        kb_id, kb_name = self._choice(session, "knowledge_bases", data.knowledge_base, user_id)
        return {
            "requestedModelId": str(model_id) if model_id else None,
            "requestedModelName": model_name,
            "knowledgeBaseId": str(kb_id) if kb_id else None,
            "knowledgeBaseName": kb_name,
            "capabilities": list(dict.fromkeys(data.tools)),
        }

    def create(self, data: NewConversationInput) -> ConversationRecord:
        """创建本用户的空会话，不导入演示历史。"""
        with self.foundation.transaction() as session:
            user_id = self.foundation.user_id(session, self.subject)
            context = self._context(session, data, user_id)
            row = (
                session.execute(
                    insert(self.conversations)
                    .values(
                        id=uuid4(),
                        user_id=user_id,
                        title=data.title,
                        model_id=context["requestedModelId"],
                        knowledge_base_id=context["knowledgeBaseId"],
                        capabilities=context["capabilities"],
                    )
                    .returning(self.conversations)
                )
                .mappings()
                .one()
            )
            result = self._record(session, dict(row))
            result.model = context["requestedModelName"]
            result.knowledge_base = context["knowledgeBaseName"]
            return result

    def _record(self, session: Session, row: dict) -> ConversationRecord:
        rows = list(
            session.execute(
                select(self.messages)
                .where(self.messages.c.conversation_id == row["id"])
                .order_by(self.messages.c.sequence_no)
            ).mappings()
        )
        attached: dict[UUID, list] = {}
        if rows:
            for file in session.execute(
                select(self.files, self.attachments.c.message_id)
                .join(self.attachments, self.attachments.c.file_id == self.files.c.id)
                .where(self.attachments.c.message_id.in_([item["id"] for item in rows]))
                .order_by(self.attachments.c.position)
            ).mappings():
                attached.setdefault(file["message_id"], []).append(
                    ConversationAttachment(
                        id=str(file["id"]), name=file["file_name"], size=file["size_bytes"]
                    )
                )
        context = rows[-1]["context_snapshot"] if rows else {}
        return ConversationRecord(
            id=str(row["id"]),
            title=row["title"],
            owner_id=self.subject,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            model=context.get("requestedModelName") or "默认模型",
            knowledge_base=context.get("knowledgeBaseName") or "",
            tools=row["capabilities"],
            version=len(rows),
            messages=[
                ConversationMessage(
                    id=str(item["id"]),
                    role=item["role"],
                    content=item["content"],
                    created_at=item["created_at"],
                    model=item["context_snapshot"].get("requestedModelName") or "默认模型",
                    knowledge_base=item["context_snapshot"].get("knowledgeBaseName") or "",
                    tools=item["context_snapshot"]["capabilities"],
                    sources=item["sources"],
                    attachments=attached.get(item["id"], []),
                    status=item["status"],
                    error_message=item["error_message"],
                )
                for item in rows
            ],
        )

    def get(self, conversation_id: str) -> ConversationRecord:
        """读取完整历史，并把超过十五分钟的未完成占位标记为中断失败。

        常规读取使用只读事务；发现超时占位后才切换写事务并重新检查状态，
        避免覆盖两次读取之间已经完成的回答。该恢复在读取时触发，不是模型超时计时器。
        """
        with self.foundation.transaction(is_readonly=True) as session:
            record = self._record(session, self._owned(session, conversation_id))
        cutoff = utc_now() - timedelta(minutes=15)
        if not any(
            item.status in {"pending", "unknown"} and item.created_at < cutoff
            for item in record.messages
        ):
            return record
        with self.foundation.transaction() as session:
            row = self._owned(session, conversation_id, lock=True)
            session.execute(
                update(self.messages)
                .where(
                    self.messages.c.conversation_id == row["id"],
                    self.messages.c.status.in_(["pending", "unknown"]),
                    self.messages.c.created_at < cutoff,
                )
                .values(
                    status="failed",
                    error_code="reply_interrupted",
                    error_message="回答等待超时或服务已中断，请重新提问",
                )
            )
            return self._record(session, self._owned(session, conversation_id))

    def list(self) -> list[ConversationSummary]:
        """一次查询最近一百条摘要，不混入固定文件样例。"""
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, self.subject)
            model = (
                select(self.messages.c.context_snapshot["requestedModelName"].as_string())
                .where(self.messages.c.conversation_id == self.conversations.c.id)
                .order_by(self.messages.c.sequence_no.desc())
                .limit(1)
                .scalar_subquery()
            )
            return [
                ConversationSummary(
                    id=str(row["id"]),
                    title=row["title"],
                    time=iso(row["updated_at"]),
                    model=row["model_name"] or "默认模型",
                )
                for row in session.execute(
                    select(self.conversations, model.label("model_name"))
                    .where(
                        self.conversations.c.user_id == user_id,
                        self.conversations.c.deleted_at.is_(None),
                    )
                    .order_by(self.conversations.c.updated_at.desc(), self.conversations.c.id)
                    .limit(100)
                ).mappings()
            ]

    def begin(self, conversation_id: str, data: ChatInput) -> tuple[ConversationRecord, str, bool]:
        """原子保存提问、附件引用和回答占位，返回历史、回答编号及是否调用模型。

        幂等键只在当前会话内识别一次发送；复用键时须逐项核对问题、资源选择、
        工具及附件。已完成请求直接返回旧回答，进行中返回 409，已失败保留原失败，
        都不会再次调用提供器。只有本次创建占位的调用者得到 should_call=True。
        """
        with self.foundation.transaction() as session:
            row = self._owned(session, conversation_id, lock=True)
            old = (
                session.execute(
                    select(self.messages).where(
                        self.messages.c.conversation_id == row["id"],
                        self.messages.c.idempotency_key == data.idempotency_key,
                    )
                )
                .mappings()
                .first()
                if data.idempotency_key
                else None
            )
            if old:
                old_files = {
                    str(key)
                    for key in session.scalars(
                        select(self.attachments.c.file_id).where(
                            self.attachments.c.message_id == old["id"]
                        )
                    )
                }
                if (
                    old["content"] != data.question
                    or data.model
                    not in {
                        old["context_snapshot"].get("requestedModelId"),
                        old["context_snapshot"].get("requestedModelName"),
                    }
                    or data.knowledge_base
                    not in {
                        old["context_snapshot"].get("knowledgeBaseId"),
                        old["context_snapshot"].get("knowledgeBaseName"),
                    }
                    or list(dict.fromkeys(data.tools)) != old["context_snapshot"]["capabilities"]
                    or old_files != set(data.attachment_ids)
                ):
                    raise HTTPException(409, "conversation_request_conflict")
                answer = (
                    session.execute(
                        select(self.messages).where(self.messages.c.reply_to_id == old["id"])
                    )
                    .mappings()
                    .one()
                )
                if answer["status"] in {"pending", "unknown"}:
                    raise HTTPException(409, "conversation_reply_in_progress")
                if answer["status"] != "completed":
                    raise HTTPException(502, answer["error_code"] or "chat_provider_unavailable")
                return self._record(session, row), str(answer["id"]), False
            # 重试使用原始请求比较；只有新提问才重新解析资源当前名称和可见范围。
            context = self._context(session, data, row["user_id"])
            if (
                session.scalar(
                    select(self.messages.c.id).where(
                        self.messages.c.conversation_id == row["id"],
                        self.messages.c.status.in_(["pending", "unknown"]),
                    )
                )
                is not None
            ):
                raise HTTPException(409, "conversation_reply_in_progress")
            sequence = session.scalar(
                select(func.coalesce(func.max(self.messages.c.sequence_no), 0)).where(
                    self.messages.c.conversation_id == row["id"]
                )
            )
            if sequence >= 200:
                raise HTTPException(409, "conversation_message_limit")
            # 问题与回答预留相邻序号；会话锁保证并发发送不会取得相同的最大序号。
            question, answer = uuid4(), uuid4()
            session.execute(
                insert(self.messages).values(
                    id=question,
                    conversation_id=row["id"],
                    sequence_no=sequence + 1,
                    role="user",
                    content=data.question,
                    status="completed",
                    idempotency_key=data.idempotency_key or uuid4(),
                    context_snapshot=context,
                )
            )
            total_size = 0
            if len(data.attachment_ids) != len(set(data.attachment_ids)):
                raise HTTPException(422, "duplicate_attachment_ids")
            for position, file_id in enumerate(data.attachment_ids, 1):
                # 上传后到发送前文件状态可能变化，因此在绑定消息的事务中重新验证并锁定。
                file = (
                    session.execute(
                        select(self.files)
                        .where(
                            self.files.c.id == identifier(file_id),
                            self.files.c.status == "ready",
                            self.files.c.uploader_id == row["user_id"],
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if file is None or not file["object_key"].startswith(f"conversations/{row['id']}/"):
                    raise HTTPException(404, "attachment_not_found")
                total_size += file["size_bytes"]
                if total_size > 20 * 1024 * 1024:
                    raise HTTPException(413, "conversation_files_too_large")
                session.execute(
                    insert(self.attachments).values(
                        id=uuid4(),
                        message_id=question,
                        file_id=file["id"],
                        position=position,
                    )
                )
            session.execute(
                insert(self.messages).values(
                    id=answer,
                    conversation_id=row["id"],
                    sequence_no=sequence + 2,
                    role="assistant",
                    reply_to_id=question,
                    content="",
                    status="pending",
                    context_snapshot=context,
                )
            )
            changes = dict(
                model_id=context["requestedModelId"],
                knowledge_base_id=context["knowledgeBaseId"],
                capabilities=context["capabilities"],
            )
            if sequence == 0:
                changes["title"] = data.question[:200]
            row = (
                session.execute(
                    update(self.conversations)
                    .where(self.conversations.c.id == row["id"])
                    .values(**changes)
                    .returning(self.conversations)
                )
                .mappings()
                .one()
            )
            return self._record(session, dict(row)), str(answer), True

    def finish(
        self, conversation_id: str, answer_id: str, reply: ChatReply | None
    ) -> ConversationRecord:
        """把仍为 pending 的占位转为完成或失败，reply=None 表示提供器失败。

        再次校验会话归属及软删除状态，并以状态条件限制 UPDATE；已取消、超时处理
        或被其他完成请求更新的回答不能被迟到结果覆盖。网络调用发生在进入本方法之前。
        """
        with self.foundation.transaction() as session:
            row = self._owned(session, conversation_id, lock=True)
            result = session.execute(
                update(self.messages)
                .where(
                    self.messages.c.id == identifier(answer_id),
                    self.messages.c.conversation_id == row["id"],
                    self.messages.c.status == "pending",
                )
                .values(
                    status="completed" if reply else "failed",
                    content=reply.text if reply else "",
                    sources=[item.model_dump(by_alias=True) for item in reply.sources]
                    if reply
                    else [],
                    error_code=None if reply else "chat_provider_unavailable",
                    error_message=None if reply else "模型服务暂不可用，请稍后重新提问",
                )
            )
            if result.rowcount != 1:
                raise HTTPException(409, "conversation_reply_changed")
            return self._record(session, self._owned(session, conversation_id))

    def delete(self, conversation_id: str) -> None:
        """先取消进行中的回答，再软删除会话，保留历史和文件引用。"""
        with self.foundation.transaction() as session:
            row = self._owned(session, conversation_id, lock=True)
            session.execute(
                update(self.messages)
                .where(
                    self.messages.c.conversation_id == row["id"],
                    self.messages.c.status.in_(["pending", "unknown"]),
                )
                .values(status="cancelled")
            )
            session.execute(
                update(self.conversations)
                .where(self.conversations.c.id == row["id"])
                .values(deleted_at=utc_now())
            )
