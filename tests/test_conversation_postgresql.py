"""只用临时 SQLite 和 PostgreSQL 方言编译检查，不连接实际 PostgreSQL 服务。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import MetaData, Table, event, inspect, select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.db.conversations import Conversation, StoredConversationMessage
from app.db.session import create_db_engine, normalize_database_url
from app.schemas.conversations import ChatInput, ConversationMessage, ConversationRecord
from app.services.conversation_store import SqlConversationStore
from app.services.conversations import service


@pytest.fixture
def migrated_database(tmp_path, monkeypatch):
    """创建独立迁移库，显式覆盖迁移账号配置，不读取开发数据库连接。"""
    url = f"sqlite:///{(tmp_path / 'postgresql-adaptation.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_MIGRATION_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_db_engine(url)
    try:
        yield engine, config
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgresql_url_uses_installed_driver_without_connecting(scheme):
    """常见连接串均使用 Psycopg 3，特殊密码及 sslmode 参数不能被二次编码或丢失。"""
    url = normalize_database_url(f"{scheme}://user:p%40ss%2Fword@localhost/db?sslmode=require")
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@ss/word"
    assert url.query["sslmode"] == "require"
    engine = create_db_engine(url.render_as_string(hide_password=False))
    try:
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()


def test_postgresql_schema_uses_native_types_and_constraints():
    """只编译目标方言，验证 JSONB、时区、消息顺序键及外键；不冒充真实库集成测试。"""
    metadata_sql = str(CreateTable(Conversation.__table__).compile(dialect=dialect()))
    message_sql = str(CreateTable(StoredConversationMessage.__table__).compile(dialect=dialect()))
    assert "tools JSONB" in metadata_sql
    assert "TIMESTAMP WITH TIME ZONE" in metadata_sql
    assert "attachments JSONB" in message_sql
    assert "PRIMARY KEY (conversation_id, position)" in message_sql
    assert "ON DELETE CASCADE" in message_sql


@pytest.mark.parametrize("existing", ["empty", "legacy", "normalized"])
def test_module_migration_preserves_shared_database_version(tmp_path, monkeypatch, existing):
    """共享库版本属于其他模块时，独立升级、重复升级及回退都不得改写其版本或数据。"""
    from sqlalchemy import text

    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{(tmp_path / 'shared.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_db_engine(url)
    shared = Config(str(root / "alembic.ini"))
    module = Config(str(root / "alembic.conversations.ini"))
    try:
        if existing != "empty":
            command.upgrade(shared, "head" if existing == "normalized" else "20260914_03")
        with engine.begin() as connection:
            if existing == "empty":
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('20260915_02')"))
            connection.execute(text("CREATE TABLE unrelated_probe (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO unrelated_probe VALUES (7)"))
        command.upgrade(module, "head")
        command.upgrade(module, "head")
        command.check(module)
        command.downgrade(module, "base")
        command.upgrade(module, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260915_02"
            )
            assert connection.scalar(text("SELECT id FROM unrelated_probe")) == 7
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version_conversations"))
                == "20260915_chat01"
            )
    finally:
        engine.dispose()


def test_full_legacy_migration_and_downgrade_preserve_new_messages(migrated_database):
    """多轮、附件、所属用户、删除标记及迁移后的续聊在升降级后都应完整保留。"""
    engine, config = migrated_database
    command.downgrade(config, "20260914_03")
    legacy = Table("demo_conversations", MetaData(), autoload_with=engine)
    now = datetime(2026, 9, 14, tzinfo=UTC)
    record = ConversationRecord(
        id="full",
        title="旧对话",
        model="模型",
        knowledge_base="知识库",
        tools=["检索"],
        owner_id="alice",
        created_at=now,
        updated_at=now,
        version=1,
        messages=[
            ConversationMessage(
                id=f"old-{role}",
                role=role,
                content=f"{role} 原文\n",
                created_at=now,
                model="模型",
                knowledge_base="知识库",
                attachments=[{"id": "attachment-" + "a" * 32, "name": "原文件.txt", "size": 7}]
                if role == "user"
                else [],
            )
            for role in ("user", "assistant")
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            legacy.insert(),
            [
                {
                    "id": record.id,
                    "created_at": now.replace(tzinfo=None),
                    "deleted": False,
                    "payload": record.model_dump_json(by_alias=True),
                },
                {
                    "id": "removed",
                    "created_at": now.replace(tzinfo=None),
                    "deleted": True,
                    "payload": "{}",
                },
            ],
        )
    command.upgrade(config, "head")
    with Session(engine) as session:
        store = SqlConversationStore(session)
        assert store.get(record.id) == record
        assert store.contains("removed") and store.get("removed") is None
        service(session, owner_id="alice").send(
            ChatInput(question="新一轮", model="模型", knowledge_base=""), record.id
        )
        newest = store.get(record.id)
        assert len(newest.messages) == 4
        assert newest.version == 2
    command.downgrade(config, "20260914_03")
    with engine.connect() as connection:
        payload = connection.scalar(select(legacy.c.payload).where(legacy.c.id == record.id))
        assert ConversationRecord.model_validate_json(payload) == newest
    command.upgrade(config, "head")
    with Session(engine) as session:
        assert SqlConversationStore(session).get(record.id) == newest
    command.check(config)


def test_invalid_legacy_record_aborts_before_replacing_tables(migrated_database):
    """坏 JSON 必须中止迁移并保留旧内容，不能静默丢掉用户历史。"""
    engine, config = migrated_database
    command.downgrade(config, "20260914_03")
    legacy = Table("demo_conversations", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            legacy.insert().values(
                id="bad",
                payload="invalid-json",
                created_at=datetime(2026, 9, 14),
                deleted=False,
            )
        )
    with pytest.raises(RuntimeError, match="Invalid legacy conversation"):
        command.upgrade(config, "head")
    assert "conversations" not in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(select(legacy.c.payload)) == "invalid-json"


def test_history_filters_before_limiting_without_loading_messages(migrated_database):
    """其他用户有超过 100 条更新的记录时，当前用户的旧会话仍应正常出现。"""
    engine, _ = migrated_database
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            Conversation.__table__.insert(),
            [
                {
                    "id": f"chat-{index}",
                    "title": str(index),
                    "model": "test",
                    "knowledge_base": "",
                    "tools": [],
                    "owner_id": "alice" if index == 0 else "bob",
                    "created_at": now,
                    "updated_at": now + timedelta(seconds=index),
                    "version": 0,
                    "deleted": False,
                }
                for index in range(102)
            ],
        )
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        """记录本次列表查询，确认查询路径不会加载或解析消息正文。"""
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with Session(engine) as session:
            assert [item.id for item in SqlConversationStore(session).list("alice")] == ["chat-0"]
            assert len(SqlConversationStore(session).list("bob")) == 100
        assert all("conversation_messages" not in statement for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_conflicting_sessions_and_message_rewrite_roll_back(migrated_database):
    """独立会话拿到同版本后只允许一个更新；篡改旧消息和删除后的迟到回复均回滚。"""
    engine, _ = migrated_database
    with Session(engine) as first_session, Session(engine) as second_session:
        answer = service(first_session).send(
            ChatInput(question="第一轮", model="test", knowledge_base="")
        )
        first = SqlConversationStore(first_session)
        second = SqlConversationStore(second_session)
        original = first.get(answer.id)
        stale = second.get(answer.id)
        updated = original.model_copy(deep=True)
        updated.version += 1
        updated.title = "已更新"
        first.save(updated, expected_version=original.version)
        stale.version += 1
        with pytest.raises(HTTPException) as conflict:
            second.save(stale, expected_version=original.version)
        assert conflict.value.status_code == 409
        altered = updated.model_copy(deep=True)
        altered.version += 1
        altered.messages[0].content = "覆盖旧内容"
        with pytest.raises(HTTPException):
            first.save(altered, expected_version=updated.version)
        assert first.get(answer.id) == updated
        first.delete(answer.id)
        with pytest.raises(HTTPException):
            second.save(altered, expected_version=updated.version)
        assert first.get(answer.id) is None
        assert list(first_session.scalars(select(StoredConversationMessage))) == []


def test_send_reads_context_once_without_reloading_for_save(migrated_database):
    """已有会话续聊只读取一次上下文；用查询次数防止远程数据库往返开销回归。"""
    engine, _ = migrated_database
    with Session(engine) as session:
        data = ChatInput(question="第一轮", model="test", knowledge_base="")
        initial = service(session).send(data)
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            """只记录 SQL 类型，不保存消息正文或查询参数。"""
            statements.append(statement.split()[0].upper())

        event.listen(engine, "before_cursor_execute", capture)
        try:
            result = service(session).send(data, initial.id)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert statements.count("SELECT") == 1
        assert statements.count("UPDATE") == 1
        assert result.conversation.version == 2
        assert len(result.conversation.messages) == 4


@pytest.mark.parametrize("preload", [False, True])
def test_save_checks_history_with_or_without_local_snapshot(migrated_database, preload):
    """直接保存也需校验旧消息；返回对象被修改时不能污染仓库用于校验的独立快照。"""
    engine, _ = migrated_database
    with Session(engine) as session:
        initial = service(session).send(
            ChatInput(question="原始问题", model="test", knowledge_base="")
        )
        store = SqlConversationStore(session)
        record = store.get(initial.id) if preload else initial.conversation.model_copy(deep=True)
        record.messages[0].content = "篡改旧消息"
        record.version += 1
        with pytest.raises(HTTPException) as conflict:
            store.save(record, expected_version=initial.conversation.version)
        assert conflict.value.status_code == 409
        assert store.get(initial.id) == initial.conversation


def test_cached_snapshot_cannot_restore_conversation_deleted_elsewhere(migrated_database):
    """另一请求删除后，本地同版本快照必须由数据库条件更新拒绝，不能复活会话。"""
    engine, _ = migrated_database
    with Session(engine) as first_session, Session(engine) as second_session:
        initial = service(first_session).send(
            ChatInput(question="原始问题", model="test", knowledge_base="")
        )
        first = SqlConversationStore(first_session)
        record = first.get(initial.id)
        SqlConversationStore(second_session).delete(initial.id)
        record.version += 1
        with pytest.raises(HTTPException) as conflict:
            first.save(record, expected_version=initial.conversation.version)
        assert conflict.value.status_code == 409
        assert first.get(initial.id) is None
