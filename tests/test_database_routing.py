from unittest.mock import Mock

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import registry

from app.core.config import Settings
from app.db.routing import (
    DatabasePools,
    ReadDatabaseUnavailable,
    ReadOnlySession,
    ReadOnlyViolation,
)
from app.db.session import create_db_engine, get_session


@pytest.fixture
def databases(tmp_path):
    metadata = MetaData()
    records = Table(
        "records", metadata, Column("id", Integer, primary_key=True), Column("label", String(64))
    )
    engines = {}
    for role in ("primary", "replica", "fallback"):
        engine = create_db_engine(f"sqlite:///{(tmp_path / f'{role}.db').as_posix()}")
        engines[role] = engine
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(records.insert().values(id=1, label=role))
    try:
        yield engines, records
    finally:
        for engine in engines.values():
            engine.dispose()


def test_writes_stay_on_primary_while_explicit_reads_use_replica(databases):
    engines, records = databases
    pools = DatabasePools(engines["primary"], engines["replica"], read_enabled=True)
    dependency = get_session(pools.primary)
    session = next(dependency)
    try:
        session.execute(records.update().values(label="written"))
        session.commit()
    finally:
        dependency.close()
    with engines["primary"].connect() as connection:
        assert connection.scalar(select(records.c.label)) == "written"
    with pools.read_session() as session:
        # 两个独立 SQLite 文件仅证明路由，不代表真实主备复制已经验证。
        assert session.scalar(select(records.c.label)) == "replica"


@pytest.mark.parametrize(
    "enabled,weight,expected",
    [
        (False, 100, "primary"),
        (True, 0, "primary"),
        (True, 100, "replica"),
    ],
)
def test_read_switch_and_weights(databases, enabled, weight, expected):
    engines, records = databases
    pools = DatabasePools(
        engines["primary"], engines["replica"], read_enabled=enabled, read_weight=weight
    )
    with pools.read_session() as session:
        assert session.scalar(select(records.c.label)) == expected


def test_intermediate_read_weight_uses_the_configured_boundary(databases, monkeypatch):
    engines, _ = databases
    pools = DatabasePools(engines["primary"], engines["replica"], read_enabled=True, read_weight=30)
    monkeypatch.setattr("app.db.routing.random.randrange", lambda size: 29)
    assert pools.choose_read_engine() is engines["replica"]
    monkeypatch.setattr("app.db.routing.random.randrange", lambda size: 30)
    assert pools.choose_read_engine() is engines["primary"]


@pytest.mark.parametrize("operation", ["insert", "update", "delete", "text", "lock"])
def test_read_session_rejects_writes_raw_sql_and_locks(databases, operation):
    engines, records = databases
    statements = {
        "insert": records.insert().values(id=2),
        "update": records.update().values(label="blocked"),
        "delete": records.delete(),
        "text": text("DELETE FROM records"),
        "lock": select(records).with_for_update(),
    }
    with ReadOnlySession(engines["primary"]) as session:
        with pytest.raises(ReadOnlyViolation):
            session.execute(statements[operation])
    with engines["primary"].connect() as connection:
        assert connection.scalar(select(records.c.label)) == "primary"


def test_read_session_rejects_orm_flush(databases):
    engines, records = databases
    mapper = registry()

    class Record:
        pass

    mapper.map_imperatively(Record, records)
    try:
        with ReadOnlySession(engines["primary"]) as session:
            record = session.get(Record, 1)
            record.label = "blocked"
            with pytest.raises(ReadOnlyViolation):
                session.commit()
        with engines["primary"].connect() as connection:
            assert connection.scalar(select(records.c.label)) == "primary"
    finally:
        mapper.dispose()


def test_replica_connection_failure_uses_only_explicit_fallback(databases, tmp_path):
    engines, records = databases
    failed = create_db_engine(f"sqlite:///{(tmp_path / 'missing' / 'replica.db').as_posix()}")
    try:
        pools = DatabasePools(engines["primary"], failed, read_enabled=True)
        with pytest.raises(ReadDatabaseUnavailable), pools.read_session():
            pytest.fail("An unavailable read connection must not be yielded")
        pools.fallback = engines["fallback"]
        with pools.read_session() as session:
            assert session.scalar(select(records.c.label)) == "fallback"
    finally:
        failed.dispose()


def test_query_failure_does_not_retry_on_another_database(databases):
    engines, _ = databases
    fallback = Mock()
    pools = DatabasePools(engines["primary"], engines["replica"], fallback, read_enabled=True)
    missing = Table("missing_table", MetaData(), Column("id", Integer))
    with pytest.raises(OperationalError), pools.read_session() as session:
        session.execute(select(missing))
    fallback.connect.assert_not_called()


@pytest.mark.parametrize(
    "role,readonly,expected",
    [
        ("primary", False, "read-write"),
        ("standby", True, "standby"),
        ("primary", True, "primary"),
    ],
)
def test_postgresql_connect_role_and_readonly_options(role, readonly, expected):
    engine = create_db_engine(
        "postgresql+psycopg://probe:placeholder@localhost/probe",
        target_role=role,
        readonly=readonly,
        connect_timeout=3,
    )

    @event.listens_for(engine, "do_connect")
    def inspect_parameters(dialect, record, args, params):
        assert params["target_session_attrs"] == expected
        assert params["connect_timeout"] == 3
        # 在真实 DBAPI connect 前截断，本用例不建立网络连接。
        raise RuntimeError("connection intercepted")

    try:
        with pytest.raises(RuntimeError, match="connection intercepted"):
            engine.connect()
        assert engine.get_execution_options().get("postgresql_readonly", False) is readonly
    finally:
        engine.dispose()


def test_partial_pool_initialization_disposes_the_primary(monkeypatch):
    primary = Mock()
    factory = Mock(side_effect=[primary, ValueError("invalid replica configuration")])
    monkeypatch.setattr("app.db.routing.create_db_engine", factory)
    settings = Settings(
        database_url="sqlite:///primary.db",
        database_read_url="sqlite:///replica.db",
        db_read_enabled=True,
    )
    with pytest.raises(ValueError, match="invalid replica"):
        DatabasePools.from_settings(settings)
    primary.dispose.assert_called_once()
