"""验证补齐历史迁移后，旧库能升级、已有 02 库不重复写入、回退不误删其他表。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def migrated_database(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'iam-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "20260915_01")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE preserved_business (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO preserved_business VALUES (7)"))
    command.upgrade(config, "20260915_02")
    try:
        yield engine, config
    finally:
        engine.dispose()


def test_iam_revision_keeps_existing_records_and_only_downgrades_its_tables(migrated_database):
    engine, config = migrated_database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO iam_identity_state VALUES ('user-1','active',1,2)"))
        connection.execute(
            text("INSERT INTO iam_identity_role VALUES ('user-1','publisher',1,1,2)")
        )
        connection.execute(
            text("INSERT INTO iam_sync_receipt VALUES ('event-1',:hash,'user',1,1,0,0,0,1,2,2)"),
            {"hash": "a" * 64},
        )
        table_names = ("iam_identity_state", "iam_identity_role", "iam_sync_receipt")
        before = {
            name: connection.execute(text(f"SELECT * FROM {name}")).all() for name in table_names
        }

    # 旧身份库已处于 02，合并后仅补会话分支，重复升级不得修改已有身份同步数据。
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260917_01"
        assert connection.scalar(text("SELECT id FROM preserved_business")) == 7
        for name, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {name}")).all() == rows

    # 回退本次汇合只恢复两条历史分支标记，不回退身份或会话数据。
    command.downgrade(config, "20260915_04")
    with engine.connect() as connection:
        assert set(connection.scalars(text("SELECT version_num FROM alembic_version"))) == {
            "20260915_04",
            "20260915_merge02",
        }
        for name, rows in before.items():
            assert connection.execute(text(f"SELECT * FROM {name}")).all() == rows
    assert {"conversations", "conversation_messages"}.issubset(inspect(engine).get_table_names())
    command.upgrade(config, "head")

    command.downgrade(config, "20260915_01")
    assert not set(table_names).intersection(inspect(engine).get_table_names())
    assert "auth_session" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM preserved_business")) == 7


@pytest.mark.parametrize("previous", ["20260915_02", "20260915_merge02"])
def test_merge_repairs_missing_session_subject_index(migrated_database, previous):
    """还原另一分支已部署但遗漏索引的旧库，升级不重建身份表或修改记录。"""
    engine, config = migrated_database
    command.upgrade(config, previous)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_auth_session_subject"))
        connection.execute(
            text("INSERT INTO iam_identity_state VALUES ('existing','disabled',1,2)")
        )

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert any(
        index["name"] == "ix_auth_session_subject" and index["column_names"] == ["subject"]
        for index in inspect(engine).get_indexes("auth_session")
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM iam_identity_state")).all() == [
            ("existing", "disabled", 1, 2)
        ]
    command.check(config)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO iam_identity_state VALUES ('user-2','unexpected',1,2)",
        "INSERT INTO iam_identity_state VALUES ('user-2','active',-1,2)",
        "INSERT INTO iam_identity_role VALUES ('missing-user','publisher',1,1,2)",
        "INSERT INTO iam_identity_role VALUES ('user-1','publisher',1,0,2)",
        "INSERT INTO iam_identity_role VALUES ('user-1','publisher',1,1,2)",
    ],
)
def test_iam_constraints_reject_invalid_sync_records(migrated_database, statement):
    engine, _ = migrated_database
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO iam_identity_state VALUES ('user-1','active',1,2)"))
        connection.execute(
            text("INSERT INTO iam_identity_role VALUES ('user-1','publisher',1,1,2)")
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(statement))
