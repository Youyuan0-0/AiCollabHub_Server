import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from legacy_auth_models import (
    AuthEventLog,
    AuthLoginRequest,
    AuthSession,
    IdentityRole,
    IdentityState,
    IdentitySyncReceipt,
    check_historical,
)
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Session

from app.db.conversations import Conversation, StoredConversationMessage
from app.db.overview import OverviewSnapshot
from app.schemas.conversations import ConversationRecord
from app.services.conversation_store import SqlConversationStore


def test_auth_migration_preserves_existing_tables_and_roundtrips(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_history (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO unrelated_history (id) VALUES (1)"))
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "migrations"))
    command.upgrade(config, "20260916_01")
    assert {"auth_session", "auth_login_request", "auth_event_log"}.issubset(
        inspect(engine).get_table_names()
    )
    check_historical(engine)
    command.downgrade(config, "20260913_01")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM unrelated_history")) == 1
    assert "auth_session" not in inspect(engine).get_table_names()
    command.upgrade(config, "20260916_01")
    engine.dispose()


@pytest.mark.parametrize(
    "legacy_revision",
    [
        "20260913_01",
        "20260914_01",
        "20260914_02",
        "20260914_03",
        "20260915_01",
        "20260915_02",
        "20260915_03",
        "20260915_04",
        "20260915_chat01",
        "20260915_merge01",
        "20260915_merge02",
        "legacy-auth",
        "legacy-combined",
    ],
)
def test_merged_migrations_preserve_both_branch_histories(tmp_path, monkeypatch, legacy_revision):
    """两个分支的旧库均可直接 upgrade head，不能跳过看板或重建已有认证数据。"""
    project = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(project / "alembic.ini"))
    engine = create_engine(url)
    tables = [
        OverviewSnapshot.__table__,
        AuthSession.__table__,
        AuthLoginRequest.__table__,
        AuthEventLog.__table__,
    ]
    # 冻结旧表定义；当前 ORM 已改为关系表，不能再用它构造历史数据库。
    old = Table(
        "demo_conversations",
        MetaData(),
        Column("id", String(64), primary_key=True),
        Column("payload", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("deleted", Boolean, nullable=False),
    )
    record = ConversationRecord(
        id="legacy-chat",
        title="历史空会话",
        model="test",
        knowledge_base="",
        owner_id="legacy-user",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
        updated_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    try:
        if legacy_revision.startswith("legacy-") or legacy_revision in {
            "20260914_03",
            "20260915_01",
        }:
            if legacy_revision == "legacy-auth":
                # 原认证分支使用同名编号，但从未执行看板建表迁移。
                command.stamp(config, "20260914_01")
            else:
                command.upgrade(config, "20260914_02")
            for table in tables[1:]:
                table.create(engine)
            if not legacy_revision.startswith("legacy-"):
                # 独立还原两次合并产生的等价旧库，不运行重排后的认证迁移来准备数据。
                command.stamp(config, legacy_revision)
        else:
            command.upgrade(config, legacy_revision)

        existing = set(inspect(engine).get_table_names())
        with engine.begin() as connection:
            if "overview_snapshots" in existing:
                connection.execute(
                    OverviewSnapshot.__table__.insert().values(
                        section="home", variant="default", payload="{}", updated_at=datetime.now()
                    )
                )
            if "demo_conversations" in existing:
                connection.execute(
                    old.insert().values(
                        id=record.id,
                        payload=record.model_dump_json(by_alias=True),
                        created_at=record.created_at.replace(tzinfo=None),
                        deleted=False,
                    )
                )
            if "auth_session" in existing:
                connection.execute(
                    AuthSession.__table__.insert().values(
                        token_hash="a" * 64,
                        ticket_hash="b" * 64,
                        scope="user",
                        subject="legacy-user",
                        display_name="历史用户",
                        created_at=1,
                        expires_at=99,
                    )
                )
                connection.execute(
                    AuthLoginRequest.__table__.insert().values(
                        state_hash="c" * 64,
                        browser_hash="d" * 64,
                        scope="user",
                        service_url="http://testserver/callback",
                        return_path="/datasets",
                        created_at=1,
                        expires_at=99,
                    )
                )
                connection.execute(
                    AuthEventLog.__table__.insert().values(
                        id="legacy-event",
                        scope="user",
                        event="login",
                        result="success",
                        request_id="legacy-request",
                        created_at=1,
                    )
                )
            preserved = {
                table: connection.execute(select(table)).mappings().all()
                for table in tables
                if table.name in existing
            }

        if "conversations" in existing:
            # 功能分支已完成分表的旧库也必须直接汇合，不能重跑搬迁或丢失已有记录。
            with Session(engine) as session:
                SqlConversationStore(session).save(record, expected_version=None)
        has_conversation = bool({"demo_conversations", "conversations"} & existing)
        command.upgrade(config, "20260916_01")
        assert {table.name for table in tables}.issubset(inspect(engine).get_table_names())
        assert {Conversation.__tablename__, StoredConversationMessage.__tablename__}.issubset(
            inspect(engine).get_table_names()
        )
        assert "demo_conversations" not in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert SqlConversationStore(session).get(record.id) == (
                record if has_conversation else None
            )
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260916_01"
            )
            for table, rows in preserved.items():
                assert connection.execute(select(table)).mappings().all() == rows
        check_historical(engine)
        # 回退先将会话重新打包为旧 JSON，同时保留全部认证与看板记录。
        command.downgrade(config, "20260914_03")
        assert {table.name for table in tables}.issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            for table, rows in preserved.items():
                assert connection.execute(select(table)).mappings().all() == rows
            if has_conversation:
                payload = connection.scalar(select(old.c.payload).where(old.c.id == record.id))
                assert ConversationRecord.model_validate_json(payload) == record
        # 重复升级不重建数据；回退认证不应删除前序看板或会话。
        command.upgrade(config, "20260916_01")
        check_historical(engine)
        command.downgrade(config, "20260914_02")
        assert {"overview_snapshots", "demo_conversations"}.issubset(
            inspect(engine).get_table_names()
        )
        command.upgrade(config, "20260916_01")
        check_historical(engine)
    finally:
        engine.dispose()


def test_repair_migration_restores_missing_conversations_without_losing_sessions(
    tmp_path, monkeypatch
):
    """旧库版本已到身份同步但缺少历史表时，升级补表且不重建已有登录记录。"""
    url = f"sqlite:///{tmp_path / 'repair.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(config, "20260915_02")
        Table("demo_conversations", MetaData(), autoload_with=engine).drop(engine)
        with engine.begin() as connection:
            connection.execute(
                AuthSession.__table__.insert().values(
                    token_hash="existing-token",
                    ticket_hash="existing-ticket",
                    scope="user",
                    subject="existing-user",
                    created_at=1,
                    expires_at=9999999999,
                )
            )
        command.upgrade(config, "20260916_01")
        check_historical(engine)
        assert "demo_conversations" not in inspect(engine).get_table_names()
        record = ConversationRecord(
            id="repaired-chat",
            title="修复后的会话",
            model="test",
            knowledge_base="",
            owner_id="existing-user",
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            updated_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        with Session(engine) as session:
            SqlConversationStore(session).save(record, expected_version=None)
        command.downgrade(config, "20260914_03")
        with engine.connect() as connection:
            payload = connection.scalar(
                text("SELECT payload FROM demo_conversations WHERE id = 'repaired-chat'")
            )
            assert ConversationRecord.model_validate_json(payload) == record
        command.upgrade(config, "20260916_01")
        with engine.connect() as connection:
            assert connection.scalar(select(AuthSession.subject)) == "existing-user"
        with Session(engine) as session:
            assert SqlConversationStore(session).get(record.id) == record
    finally:
        engine.dispose()


def test_merged_migrations_have_one_head_and_support_postgresql_offline_sql(monkeypatch):
    """汇合后只有一个 head；历史认证支持离线 SQL，会话数据搬迁明确要求在线连接。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/migration_test")
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    assert ScriptDirectory.from_config(config).get_heads() == ["20260917_auth01"]
    command.upgrade(config, "20260915_02", sql=True)
    sql = output.getvalue()
    for table in ("overview_snapshots", "auth_session", "auth_login_request", "auth_event_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE TABLE demo_conversations" in sql
    for table in ("iam_identity_state", "iam_identity_role", "iam_sync_receipt"):
        assert f"CREATE TABLE {table}" in sql
    with pytest.raises(RuntimeError, match="requires an online database connection"):
        command.upgrade(config, "head", sql=True)


@pytest.mark.parametrize("previous", ["20260915_01", "20260915_02", "20260915_03"])
def test_global_upgrade_adopts_independent_conversations_and_preserves_identity(
    tmp_path, monkeypatch, previous
):
    """先独立迁移会话的共享库也能升级全部模块，不重复建表或丢失身份状态。"""
    root = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'independent.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    shared = Config(str(root / "alembic.ini"))
    module = Config(str(root / "alembic.conversations.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(shared, previous)
        command.upgrade(module, "head")
        existing = set(inspect(engine).get_table_names())
        with engine.begin() as connection:
            connection.execute(
                AuthSession.__table__.insert().values(
                    token_hash="preserved-token",
                    ticket_hash="preserved-ticket",
                    scope="platform",
                    subject="preserved-user",
                    created_at=1,
                    expires_at=9999999999,
                )
            )
            if "iam_identity_state" in existing:
                connection.execute(
                    IdentityState.__table__.insert().values(
                        subject="preserved-user",
                        status="disabled",
                        sync_time=3,
                        updated_at=4,
                    )
                )
                connection.execute(
                    IdentityRole.__table__.insert().values(
                        subject="preserved-user",
                        role_code="platform_admin",
                        authorized=False,
                        sync_time=3,
                        updated_at=4,
                    )
                )
                connection.execute(
                    IdentitySyncReceipt.__table__.insert().values(
                        event_id="preserved-event",
                        payload_hash="f" * 64,
                        resource_type="user",
                        sync_time=3,
                        applied=1,
                        ignored=0,
                        unchanged=0,
                        revoked_sessions=1,
                        attempts=1,
                        received_at=4,
                        last_received_at=4,
                    )
                )
            preserved = {
                model: connection.execute(select(model)).mappings().all()
                for model in (AuthSession, IdentityState, IdentityRole, IdentitySyncReceipt)
                if model.__tablename__ in existing
            }
        record = ConversationRecord(
            id="module-chat",
            title="独立迁移后的会话",
            model="test",
            knowledge_base="",
            owner_id="preserved-user",
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            updated_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        with Session(engine) as session:
            SqlConversationStore(session).save(record, expected_version=None)
        command.upgrade(shared, "20260916_01")
        command.upgrade(shared, "20260916_01")
        check_historical(engine)
        command.check(module)
        assert "demo_conversations" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260916_01"
            )
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version_conversations"))
                == "20260915_chat01"
            )
            for model, rows in preserved.items():
                assert connection.execute(select(model)).mappings().all() == rows
        with Session(engine) as session:
            assert SqlConversationStore(session).get(record.id) == record
    finally:
        engine.dispose()


def test_merge_validation_uses_frozen_schema_instead_of_future_orm(tmp_path, monkeypatch):
    """业务模型未来添加字段时，旧迁移仍按自己的版本校验，不提前要求新列。"""
    url = f"sqlite:///{tmp_path / 'frozen.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(config, "20260915_merge01")
        future_table = Conversation.__table__.to_metadata(MetaData())
        future_table.append_column(Column("future_feature", Text))
        monkeypatch.setattr(Conversation, "__table__", future_table)
        command.upgrade(config, "20260916_01")
        assert "future_feature" not in {
            column["name"] for column in inspect(engine).get_columns("conversations")
        }
    finally:
        engine.dispose()


def test_merge_does_not_discard_legacy_data_when_both_stores_exist(tmp_path, monkeypatch):
    """补表分支出现新的旧格式记录时中止，不能当作空表删除或覆盖关系表。"""
    url = f"sqlite:///{tmp_path / 'ambiguous.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    engine = create_engine(url)
    try:
        command.upgrade(config, "20260915_merge01")
        record = ConversationRecord(
            id="normalized-chat",
            title="关系表中的会话",
            model="test",
            knowledge_base="",
            created_at=datetime(2026, 9, 15, tzinfo=UTC),
            updated_at=datetime(2026, 9, 15, tzinfo=UTC),
        )
        with Session(engine) as session:
            SqlConversationStore(session).save(record, expected_version=None)
        command.upgrade(config, "20260915_03")
        old = Table("demo_conversations", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(
                old.insert().values(
                    id="legacy-only",
                    payload='{"preserved":true}',
                    created_at=datetime(2026, 9, 15),
                    deleted=False,
                )
            )
        with pytest.raises(RuntimeError, match="Ambiguous conversation tables"):
            command.upgrade(config, "20260916_01")
        with engine.connect() as connection:
            assert connection.scalar(select(old.c.payload)) == '{"preserved":true}'
        with Session(engine) as session:
            assert SqlConversationStore(session).get(record.id) == record
    finally:
        engine.dispose()


@pytest.mark.parametrize("legacy", ["auth", "overview", "conversations"])
def test_merge_upgrades_both_existing_branches_without_losing_data(tmp_path, monkeypatch, legacy):
    """旧分支共用 01 版本，按已有表补齐新链，不能只验证空库成功。"""
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_MIGRATION_URL", url)
    engine = create_engine(url)
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    if legacy == "auth":
        for model in (AuthSession, AuthLoginRequest, AuthEventLog):
            model.__table__.create(engine)
        table, key, value = "auth_session", "subject", "existing-user"
        with engine.begin() as connection:
            connection.execute(
                AuthSession.__table__.insert().values(
                    token_hash="old-token",
                    ticket_hash="old-ticket",
                    scope="user",
                    subject=value,
                    created_at=1,
                    expires_at=9999999999,
                )
            )
    else:
        OverviewSnapshot.__table__.create(engine)
        table, key, value = "overview_snapshots", "payload", '{"existing":true}'
        with engine.begin() as connection:
            connection.execute(
                OverviewSnapshot.__table__.insert().values(
                    section="home",
                    variant="default",
                    payload=value,
                    updated_at=datetime(2026, 9, 14),
                )
            )
        if legacy == "conversations":
            # 固定旧表定义，不能用已升级的 ORM 来伪造旧数据库。
            old = Table(
                "demo_conversations",
                MetaData(),
                Column("id", String(64), primary_key=True),
                Column("payload", Text, nullable=False),
                Column("created_at", DateTime, nullable=False),
                Column("deleted", Boolean, nullable=False),
            )
            old.create(engine)
            with engine.begin() as connection:
                connection.execute(
                    old.insert().values(
                        id="existing-chat",
                        payload=json.dumps(
                            {
                                "id": "existing-chat",
                                "title": "原有空会话",
                                "model": "test",
                                "knowledgeBase": "",
                                "createdAt": "2026-09-14T00:00:00Z",
                                "updatedAt": "2026-09-14T00:00:00Z",
                                "messages": [],
                            }
                        ),
                        created_at=datetime(2026, 9, 14),
                        deleted=False,
                    )
                )
    command.stamp(config, "20260914_02" if legacy == "conversations" else "20260914_01")
    command.upgrade(config, "20260916_01")
    assert {
        "overview_snapshots",
        "conversations",
        "conversation_messages",
        "auth_session",
        "auth_login_request",
        "auth_event_log",
    }.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT {key} FROM {table}")) == value
        if legacy == "conversations":
            # 新迁移将旧 JSON 搬到关系表，原有会话编号仍然可读。
            assert connection.scalar(text("SELECT id FROM conversations")) == "existing-chat"
    check_historical(engine)
    command.upgrade(config, "20260916_01")
    engine.dispose()
