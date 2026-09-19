from datetime import UTC, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import create_db_engine
from app.modules.common.auth.models import User
from app.modules.common.system_logs import SystemLog, SystemLogLevel

PARENT_REVISION = "20260917_merge_ds_auth"
LOG_REVISION = "20260918_01"
LEVEL_REVISION = "20260918_02"


def migration_config() -> Config:
    """读取正式迁移链，测试连接由临时环境配置覆盖。"""
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


@pytest.fixture
def migrated_engine(tmp_path, monkeypatch):
    """从完整迁移链创建隔离数据库，验证真实迁移而非 create_all。"""
    url = f"sqlite:///{(tmp_path / 'system-logs.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(migration_config(), PARENT_REVISION)
    engine = create_db_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_upgrade_and_downgrade_preserve_existing_tables_and_data(migrated_engine):
    user_id = uuid4()
    with Session(migrated_engine) as session:
        session.add(User(id=user_id, iam_subject="system-log-migration-test"))
        session.commit()
    before = set(inspect(migrated_engine).get_table_names())

    command.upgrade(migration_config(), LOG_REVISION)
    inspector = inspect(migrated_engine)
    assert set(inspector.get_table_names()) == before | {"system_logs"}
    assert inspector.get_foreign_keys("system_logs") == []
    indexes = {index["name"]: index for index in inspector.get_indexes("system_logs")}
    assert indexes["ix_system_logs_created_at"]["column_names"] == ["created_at"]
    assert indexes["ix_system_logs_request_id"]["column_names"] == ["request_id"]
    assert not indexes["ix_system_logs_request_id"]["unique"]

    command.downgrade(migration_config(), PARENT_REVISION)
    assert set(inspect(migrated_engine).get_table_names()) == before
    with Session(migrated_engine) as session:
        assert session.get(User, user_id).iam_subject == "system-log-migration-test"
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == PARENT_REVISION


def test_logs_share_request_id_and_support_background_events(migrated_engine):
    command.upgrade(migration_config(), LOG_REVISION)
    request_id = uuid4()
    occurred_at = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    with Session(migrated_engine) as session:
        first = SystemLog(
            level=SystemLogLevel.WARNING,
            source="auth",
            event="iam_validation_failed",
            message="IAM validation failed",
            request_id=request_id,
            details={"errorType": "TimeoutError", "retryCount": 1},
            created_at=occurred_at,
        )
        second = SystemLog(
            level=SystemLogLevel.ERROR,
            source="auth",
            event="login_failed",
            message="Login failed",
            request_id=request_id,
        )
        background = SystemLog(
            level=SystemLogLevel.INFO,
            source="common",
            event="cleanup_completed",
            message="Cleanup completed",
        )
        session.add_all([first, second, background])
        session.commit()
        session.expire_all()

        assert (
            len(session.scalars(select(SystemLog).where(SystemLog.request_id == request_id)).all())
            == 2
        )
        assert first.level is SystemLogLevel.WARNING
        assert first.details == {"errorType": "TimeoutError", "retryCount": 1}
        assert first.created_at == occurred_at.astimezone(UTC)
        assert background.request_id is None
        assert second.details == background.details == {}
        assert second.details is not background.details
        assert background.created_at.tzinfo == UTC
        assert len({first.id, second.id, background.id}) == 3


def test_database_defaults_and_level_constraint_apply_without_orm(migrated_engine):
    command.upgrade(migration_config(), LOG_REVISION)
    statement = text(
        "INSERT INTO system_logs (id, level, source, event, message) "
        "VALUES (:id, :level, 'common', 'probe', 'Probe')"
    )
    record_id = uuid4()
    with migrated_engine.begin() as connection:
        connection.execute(statement, {"id": record_id.hex, "level": "info"})
    with Session(migrated_engine) as session:
        record = session.get(SystemLog, record_id)
        assert record.details == {}
        assert record.created_at.tzinfo == UTC

    with pytest.raises(IntegrityError, match="ck_system_logs_level"):
        with migrated_engine.begin() as connection:
            connection.execute(statement, {"id": uuid4().hex, "level": "invalid"})


def test_postgresql_migration_emits_json_object_constraint_and_time_zone(monkeypatch):
    # 只编译新增迁移，不连接真实服务，也不执行历史数据搬迁。
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/system_logs_test")
    config = migration_config()
    config.output_buffer = StringIO()
    command.upgrade(config, f"{PARENT_REVISION}:{LOG_REVISION}", sql=True)
    sql = config.output_buffer.getvalue()
    assert "details JSONB DEFAULT '{}' NOT NULL" in sql
    assert "CHECK (jsonb_typeof(details) = 'object')" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "WHERE request_id IS NOT NULL" in sql
    assert "DROP TABLE" not in sql


def test_level_migration_preserves_history_and_enforces_three_levels(migrated_engine):
    """历史五级数据无损归类后只能写三级，回退约束不删除任何记录。"""
    command.upgrade(migration_config(), LOG_REVISION)
    insert_log = text(
        "INSERT INTO system_logs (id, level, source, event, message) "
        "VALUES (:id, :level, 'common', 'legacy', 'Original message')"
    )
    with migrated_engine.begin() as connection:
        for level in ["debug", "info", "warning", "error", "critical"]:
            connection.execute(insert_log, {"id": uuid4().hex, "level": level})
    command.upgrade(migration_config(), LEVEL_REVISION)
    with Session(migrated_engine) as session:
        records = session.scalars(select(SystemLog)).all()
        assert len(records) == 5
        assert sorted(record.level for record in records) == [
            "error",
            "error",
            "info",
            "info",
            "warning",
        ]
        assert all(record.message == "Original message" for record in records)
    for level in ["debug", "critical", "invalid"]:
        with pytest.raises(IntegrityError, match="ck_system_logs_level"):
            with migrated_engine.begin() as connection:
                connection.execute(insert_log, {"id": uuid4().hex, "level": level})
    command.downgrade(migration_config(), LOG_REVISION)
    with migrated_engine.begin() as connection:
        assert connection.scalar(text("SELECT count(*) FROM system_logs")) == 5
        connection.execute(insert_log, {"id": uuid4().hex, "level": "critical"})


def test_postgresql_level_migration_compiles_without_recreating_table(monkeypatch):
    """生产数据库仅更新历史级别和约束，避免整表重建。"""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost/system_logs_test")
    config = migration_config()
    config.output_buffer = StringIO()
    command.upgrade(config, f"{LOG_REVISION}:{LEVEL_REVISION}", sql=True)
    sql = config.output_buffer.getvalue()
    assert "UPDATE system_logs SET level = 'info' WHERE level = 'debug'" in sql
    assert "CHECK (level IN ('info', 'warning', 'error'))" in sql
    assert "DROP TABLE" not in sql and "CREATE TABLE" not in sql
