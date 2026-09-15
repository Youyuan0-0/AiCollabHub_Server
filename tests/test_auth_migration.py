from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text

from app.db.conversations import DemoConversation
from app.db.overview import OverviewSnapshot
from app.modules.common.auth.models import AuthEventLog, AuthLoginRequest, AuthSession


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
    command.upgrade(config, "head")
    assert {"auth_session", "auth_login_request", "auth_event_log"}.issubset(
        inspect(engine).get_table_names()
    )
    command.check(config)
    command.downgrade(config, "20260913_01")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM unrelated_history")) == 1
    assert "auth_session" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    engine.dispose()


@pytest.mark.parametrize(
    "legacy_revision",
    [
        "20260913_01",
        "20260914_01",
        "20260914_02",
        "20260914_03",
        "20260915_01",
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
        DemoConversation.__table__,
        AuthSession.__table__,
        AuthLoginRequest.__table__,
        AuthEventLog.__table__,
    ]
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
            for table in tables[2:]:
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
                    DemoConversation.__table__.insert().values(
                        id="legacy-chat", payload="{}", created_at=datetime.now(), deleted=False
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

        command.upgrade(config, "head")
        assert {table.name for table in tables}.issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260915_02"
            )
            for table, rows in preserved.items():
                assert connection.execute(select(table)).mappings().all() == rows
        command.check(config)
        # 两个已使用的认证版本结构相同，退回远端版本也必须保留全部认证记录。
        command.downgrade(config, "20260914_03")
        assert {table.name for table in tables}.issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            for table, rows in preserved.items():
                assert connection.execute(select(table)).mappings().all() == rows
        # 重复升级不重建数据；回退认证不应删除前序看板或会话。
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "20260914_02")
        assert {"overview_snapshots", "demo_conversations"}.issubset(
            inspect(engine).get_table_names()
        )
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()


def test_merged_migrations_have_one_head_and_support_postgresql_offline_sql(monkeypatch):
    """离线生成 PostgreSQL SQL，不连接开发数据库，也不需要手工调整历史版本号。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/migration_test")
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    assert ScriptDirectory.from_config(config).get_heads() == ["20260915_02"]
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    for table in ("overview_snapshots", "auth_session", "auth_login_request", "auth_event_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "CREATE TABLE demo_conversations" in sql


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
            DemoConversation.__table__.create(engine)
            with engine.begin() as connection:
                connection.execute(
                    DemoConversation.__table__.insert().values(
                        id="existing-chat",
                        payload='{"messages":[]}',
                        created_at=datetime(2026, 9, 14),
                        deleted=False,
                    )
                )
    command.stamp(config, "20260914_02" if legacy == "conversations" else "20260914_01")
    command.upgrade(config, "head")
    assert {
        "overview_snapshots",
        "demo_conversations",
        "auth_session",
        "auth_login_request",
        "auth_event_log",
    }.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT {key} FROM {table}")) == value
        if legacy == "conversations":
            assert connection.scalar(text("SELECT id FROM demo_conversations")) == "existing-chat"
    command.check(config)
    command.upgrade(config, "head")
    engine.dispose()
