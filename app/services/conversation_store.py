"""临时会话存储边界；复用现有 JSON 表，未来可替换为正式会话表与消息表。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.conversations import DemoConversation
from app.schemas.conversations import ChatAnswer, ConversationMessage, ConversationRecord


class ConversationStore(Protocol):
    """服务层仅依赖此协议；None 表示不存在或已删，历史样例是否隐藏由 contains 判断。"""

    def get(self, identifier: str) -> ConversationRecord | None: ...
    def contains(self, identifier: str) -> bool: ...
    def list(self) -> list[ConversationRecord]: ...
    def save(self, record: ConversationRecord, expected_version: int | None) -> None: ...
    def delete(self, identifier: str) -> None: ...


def record_from_answer(answer: ChatAnswer, created_at: datetime) -> ConversationRecord:
    """将旧单轮记录转换为完整会话，兼容已有数据，无需批量改写历史。"""
    return ConversationRecord(
        id=answer.id,
        title=answer.question[:200],
        model=answer.model,
        knowledge_base=answer.knowledge_base,
        created_at=created_at,
        updated_at=created_at,
        messages=[
            ConversationMessage(
                id=f"{answer.id}-{role}",
                role=role,
                content=content,
                created_at=created_at,
                model=answer.model,
                knowledge_base=answer.knowledge_base,
                sources=sources,
            )
            for role, content, sources in (
                ("user", answer.question, []),
                ("assistant", answer.text, answer.sources),
            )
        ],
    )


@contextmanager
def database_operation(session: Session):
    """数据库失败时回滚，隐藏连接串和 SQL；并发首次创建以 409 告知调用方。"""
    try:
        yield
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conversation_write_conflict") from None
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(503, "conversations_unavailable") from None


class SqlConversationStore:
    """当前临时实现：一个会话一个 JSON 记录，删除保留标记，避免旧请求复活已删会话。"""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def decode(row: DemoConversation) -> ConversationRecord:
        """先读取新结构，再兼容旧 ChatAnswer；两种格式都无效时返回 503。"""
        try:
            return ConversationRecord.model_validate_json(row.payload)
        except ValidationError:
            try:
                return record_from_answer(
                    ChatAnswer.model_validate_json(row.payload), row.created_at.replace(tzinfo=UTC)
                )
            except ValidationError:
                raise HTTPException(503, "conversation_record_invalid") from None

    def get(self, identifier: str) -> ConversationRecord | None:
        """查询独立会话对象；读取后结束事务，避免第三方调用期间持有数据库事务。"""
        with database_operation(self.session):
            row = self.session.get(DemoConversation, identifier)
            result = self.decode(row) if row is not None and not row.deleted else None
            self.session.rollback()
            return result

    def contains(self, identifier: str) -> bool:
        """检查是否已有记录或删除标记，供服务层决定是否使用固定历史样例。"""
        with database_operation(self.session):
            exists = self.session.get(DemoConversation, identifier) is not None
            self.session.rollback()
            return exists

    def list(self) -> list[ConversationRecord]:
        """只读出未删除的会话，按更新时间排序；当前完整 JSON 存储适用于开发规模。"""
        with database_operation(self.session):
            rows = self.session.scalars(
                select(DemoConversation).where(DemoConversation.deleted.is_(False))
            ).all()
            result = [self.decode(row) for row in rows]
            self.session.rollback()
            return sorted(result, key=lambda record: (record.updated_at, record.id), reverse=True)

    def save(self, record: ConversationRecord, expected_version: int | None) -> None:
        """None 表示首次创建；更新比较旧版本和 JSON，拒绝并发覆盖及删除后的迟到结果。"""
        with database_operation(self.session):
            row = self.session.get(DemoConversation, record.id)
            payload = record.model_dump_json(by_alias=True)
            if expected_version is None:
                if row is not None:
                    raise HTTPException(409, "conversation_write_conflict")
                self.session.add(
                    DemoConversation(
                        id=record.id,
                        payload=payload,
                        created_at=record.created_at.replace(tzinfo=None),
                        deleted=False,
                    )
                )
            else:
                if row is None or row.deleted or self.decode(row).version != expected_version:
                    raise HTTPException(409, "conversation_write_conflict")
                # 条件更新同时抵御读取后发生的其他更新；失败时不保留半轮消息。
                result = self.session.execute(
                    update(DemoConversation)
                    .where(
                        DemoConversation.id == record.id,
                        DemoConversation.payload == row.payload,
                        DemoConversation.deleted.is_(False),
                    )
                    .values(payload=payload)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    self.session.rollback()
                    raise HTTPException(409, "conversation_write_conflict")
            self.session.commit()

    def delete(self, identifier: str) -> None:
        """内容清空并保留删除标记，完整删除消息且防止固定历史重新出现。"""
        with database_operation(self.session):
            row = self.session.get(DemoConversation, identifier)
            if row is None:
                row = DemoConversation(
                    id=identifier, created_at=datetime.now(UTC).replace(tzinfo=None)
                )
                self.session.add(row)
            row.deleted = True
            row.payload = "{}"
            self.session.commit()
