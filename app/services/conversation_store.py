"""会话存储协议及 PostgreSQL 实现；业务层无需处理表结构或事务细节。"""

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.conversations import Conversation, StoredConversationMessage
from app.schemas.conversations import (
    ChatAnswer,
    ConversationMessage,
    ConversationRecord,
    ConversationSummary,
)


class ConversationStore(Protocol):
    """服务层仅依赖此协议；None 表示不存在或已删，历史样例是否隐藏由 contains 判断。"""

    def get(self, identifier: str) -> ConversationRecord | None: ...
    def contains(self, identifier: str) -> bool: ...
    def list(self, owner_id: str = "") -> list[ConversationSummary]: ...
    def save(self, record: ConversationRecord, expected_version: int | None) -> None: ...
    def delete(self, identifier: str) -> None: ...


def record_from_answer(answer: ChatAnswer, created_at: datetime) -> ConversationRecord:
    """将文件中的单轮固定样例转为完整会话；数据库内的旧记录由 Alembic 负责迁移。"""
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
    except HTTPException:
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "conversation_write_conflict") from None
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(503, "conversations_unavailable") from None


def utc_time(value: datetime) -> datetime:
    """统一返回 UTC；SQLite 读出的无时区时间按旧约定解释为 UTC。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SqlConversationStore:
    """会话与消息分表存储；先比较版本并锁定会话行，再原子追加整轮消息。"""

    def __init__(self, session: Session):
        self.session = session
        # 仅保留本仓库实例最近一次读取的独立快照；用于校验旧消息，不能替代数据库版本条件更新。
        self._loaded_record: ConversationRecord | None = None

    @staticmethod
    def decode(row: Conversation, messages: list[StoredConversationMessage]) -> ConversationRecord:
        """组装独立的接口对象；损坏字段统一返回 503，不暴露 SQL 或数据库内容。"""
        try:
            return ConversationRecord(
                id=row.id,
                title=row.title,
                model=row.model,
                knowledge_base=row.knowledge_base,
                tools=row.tools,
                owner_id=row.owner_id,
                created_at=utc_time(row.created_at),
                updated_at=utc_time(row.updated_at),
                version=row.version,
                messages=[
                    ConversationMessage(
                        id=item.id,
                        role=item.role,
                        content=item.content,
                        created_at=utc_time(item.created_at),
                        model=item.model,
                        knowledge_base=item.knowledge_base,
                        tools=item.tools,
                        sources=item.sources,
                        attachments=item.attachments,
                    )
                    for item in messages
                ],
            )
        except ValidationError:
            raise HTTPException(503, "conversation_record_invalid") from None

    def get(self, identifier: str) -> ConversationRecord | None:
        """一次关联查询读取一致的摘要和消息；返回前释放事务，第三方调用不占锁。"""
        with database_operation(self.session):
            # 单条 SQL 避免 READ COMMITTED 下两次查询之间续聊或删除，读到不同版本。
            rows = self.session.execute(
                select(Conversation, StoredConversationMessage)
                .outerjoin(
                    StoredConversationMessage,
                    StoredConversationMessage.conversation_id == Conversation.id,
                )
                .where(Conversation.id == identifier, Conversation.deleted.is_(False))
                .order_by(StoredConversationMessage.position)
            ).all()
            result = (
                self.decode(rows[0][0], [item for _, item in rows if item is not None])
                if rows
                else None
            )
            self.session.rollback()
            self._loaded_record = result.model_copy(deep=True) if result is not None else None
            return result

    def contains(self, identifier: str) -> bool:
        """包含删除标记的存在性检查，避免固定样例在删除后再次出现。"""
        with database_operation(self.session):
            exists = (
                self.session.scalar(select(Conversation.id).where(Conversation.id == identifier))
                is not None
            )
            self.session.rollback()
            return exists

    def list(self, owner_id: str = "") -> list[ConversationSummary]:
        """在数据库按可信用户过滤、排序并限制 100 条；列表不加载消息正文。"""
        with database_operation(self.session):
            rows = self.session.execute(
                select(
                    Conversation.id, Conversation.title, Conversation.model, Conversation.updated_at
                )
                .where(
                    Conversation.deleted.is_(False),
                    or_(Conversation.owner_id == "", Conversation.owner_id == owner_id),
                )
                .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
                .limit(100)
            ).all()
            result = [
                ConversationSummary(
                    id=row.id,
                    title=row.title,
                    model=row.model,
                    time=utc_time(row.updated_at).isoformat(),
                )
                for row in rows
            ]
            self.session.rollback()
            return result

    def save(self, record: ConversationRecord, expected_version: int | None) -> None:
        """None 表示首次创建；续聊使用旧版本条件更新，只追加消息且不改写既有内容。"""
        with database_operation(self.session):
            values = {
                "title": record.title,
                "model": record.model,
                "knowledge_base": record.knowledge_base,
                "tools": list(record.tools),
                "updated_at": utc_time(record.updated_at),
                "version": record.version,
            }
            existing_count = 0
            if expected_version is None:
                self.session.add(
                    Conversation(
                        id=record.id,
                        owner_id=record.owner_id,
                        created_at=utc_time(record.created_at),
                        deleted=False,
                        **values,
                    )
                )
                # 先插入父表，确保 PostgreSQL 外键校验通过；后续失败会整体回滚。
                self.session.flush()
            else:
                if record.version != expected_version + 1:
                    raise HTTPException(409, "conversation_write_conflict")
                # 复用读取上下文时的快照，省去保存前重复读取摘要和全部消息的两次数据库往返。
                # 独立调用 save 或版本不匹配时补查；深拷贝确保调用方修改旧消息仍能被发现。
                previous = self._loaded_record
                if (
                    previous is None
                    or previous.id != record.id
                    or previous.version != expected_version
                ):
                    previous = self.get(record.id)
                if (
                    previous is None
                    or previous.version != expected_version
                    or previous.owner_id != record.owner_id
                    or previous.messages != record.messages[: len(previous.messages)]
                ):
                    raise HTTPException(409, "conversation_write_conflict")
                existing_count = len(previous.messages)
                result = self.session.execute(
                    update(Conversation)
                    .where(
                        Conversation.id == record.id,
                        Conversation.owner_id == record.owner_id,
                        Conversation.version == expected_version,
                        Conversation.deleted.is_(False),
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise HTTPException(409, "conversation_write_conflict")
                # 版本匹配才追加并持有行锁到提交；其他请求续聊或删除后，旧快照仍返回 409。
            for position, message in enumerate(
                record.messages[existing_count:], start=existing_count
            ):
                self.session.add(
                    StoredConversationMessage(
                        conversation_id=record.id,
                        position=position,
                        id=message.id,
                        role=message.role,
                        content=message.content,
                        created_at=utc_time(message.created_at),
                        model=message.model,
                        knowledge_base=message.knowledge_base,
                        tools=list(message.tools),
                        sources=[source.model_dump(mode="json") for source in message.sources],
                        attachments=[
                            attachment.model_dump(mode="json") for attachment in message.attachments
                        ],
                    )
                )
            self.session.commit()
            self._loaded_record = record.model_copy(deep=True)

    def delete(self, identifier: str) -> None:
        """同一事务清除全部消息及摘要，保留空删除标记；固定样例首次删除也保存标记。"""
        with database_operation(self.session):
            now = datetime.now(UTC)
            result = self.session.execute(
                update(Conversation)
                .where(Conversation.id == identifier)
                .values(
                    deleted=True,
                    title="",
                    model="",
                    knowledge_base="",
                    tools=[],
                    updated_at=now,
                    version=Conversation.version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount == 0:
                self.session.add(
                    Conversation(
                        id=identifier,
                        title="",
                        model="",
                        knowledge_base="",
                        tools=[],
                        owner_id="",
                        created_at=now,
                        updated_at=now,
                        version=0,
                        deleted=True,
                    )
                )
                self.session.flush()
            # 显式删除消息不依赖 SQLite 的外键开关；附件原文件仍遵循现有仓库生命周期。
            self.session.execute(
                delete(StoredConversationMessage).where(
                    StoredConversationMessage.conversation_id == identifier
                )
            )
            self.session.commit()
