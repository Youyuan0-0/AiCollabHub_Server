import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
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
    text,
)


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


@pytest.mark.parametrize("legacy", ["auth", "overview", "conversations"])
def test_merge_upgrades_both_existing_branches_without_losing_data(tmp_path, monkeypatch, legacy):
    """旧分支共用 01 版本，按已有表补齐新链，不能只验证空库成功。"""
    from datetime import datetime

    from app.db.overview import OverviewSnapshot
    from app.modules.common.auth.models import AuthEventLog, AuthLoginRequest, AuthSession

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
    command.upgrade(config, "head")
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
    command.check(config)
    command.upgrade(config, "head")
    engine.dispose()
