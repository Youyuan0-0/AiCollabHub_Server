import os
from datetime import datetime
from decimal import Decimal
from io import StringIO
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Unicode,
    UnicodeText,
    func,
    inspect,
    literal,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import registry
from sqlalchemy.schema import CreateTable

from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_db_engine, get_session
from app.main import create_app


def make_probe_table(name):
    # 独立元数据不会把验证表注册进业务 Base 或正式迁移历史。
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)
    table = Table(
        name,
        metadata,
        Column("id", Integer, Identity(), primary_key=True),
        Column("code", String(64), nullable=False, unique=True),
        Column("title", Unicode(128), nullable=False),
        Column("body", UnicodeText),
        Column("amount", Numeric(18, 4)),
        Column("created_at", DateTime),
        Column("active", Boolean),
        Column("attachment", LargeBinary),
    )
    Index(f"ix_{name}_title", table.c.title)
    return table


@pytest.fixture(scope="module")
def offline_engine():
    # 创建引擎会加载真实驱动，但不会连接这个占位地址。
    engine = create_db_engine("dm+dmPython://compat_probe:placeholder@127.0.0.1:5236")
    try:
        yield engine
    finally:
        engine.dispose()


def test_native_driver_and_dialect_load(offline_engine):
    import dmPython
    import dmSQLAlchemy

    assert dmPython.apilevel == "2.0"
    assert offline_engine.dialect.dbapi is dmPython
    assert offline_engine.dialect.name == "dm"
    assert offline_engine.dialect.driver == "dmPython"
    assert dmSQLAlchemy.dialect is type(offline_engine.dialect)


def test_health_query_compiles_for_dm(offline_engine):
    sql = str(select(literal(1)).compile(dialect=offline_engine.dialect))
    assert "FROM DUAL" in sql.upper()


def test_common_types_and_explicit_identity_compile(offline_engine):
    table = make_probe_table("aih_dm_offline")
    sql = str(CreateTable(table).compile(dialect=offline_engine.dialect))
    assert "IDENTITY" in sql.upper()
    assert "NUMERIC(18, 4)" in sql.upper()
    assert "PRIMARY KEY" in sql.upper()


def test_default_integer_primary_key_does_not_imply_identity(offline_engine):
    # 新版方言不再将普通 Integer 主键自动变成自增列，模型需要显式声明。
    table = Table("aih_dm_plain_pk", MetaData(), Column("id", Integer, primary_key=True))
    sql = str(CreateTable(table).compile(dialect=offline_engine.dialect))
    assert "IDENTITY" not in sql.upper()


@pytest.mark.parametrize("operation", ["insert", "update", "delete", "select"])
def test_crud_uses_bound_parameters(offline_engine, operation):
    table = make_probe_table("aih_dm_bound")
    title = "中文参数 O'Reilly"
    statements = {
        "insert": table.insert().values(code="one", title=title),
        "update": table.update().where(table.c.code == "one").values(title=title),
        "delete": table.delete().where(table.c.title == title),
        "select": select(table).where(table.c.title == title),
    }
    compiled = statements[operation].compile(dialect=offline_engine.dialect)
    assert title not in str(compiled)
    assert title in compiled.params.values()
    assert "?" in str(compiled)


def test_ordered_pagination_compiles(offline_engine):
    table = make_probe_table("aih_dm_page")
    statement = select(table.c.id).order_by(table.c.id).offset(10).limit(20)
    compiled = statement.compile(dialect=offline_engine.dialect)
    sql = str(compiled).upper()
    assert "ORDER BY" in sql and "OFFSET" in sql and "FETCH" in sql
    assert {10, 20}.issubset(set(compiled.params.values()))


def test_alembic_can_generate_dm_migration_sql(offline_engine):
    output = StringIO()
    # 保留真实失败，不注册临时 Oracle/DefaultImpl 来掩盖缺失的达梦迁移适配。
    context = MigrationContext.configure(
        dialect=offline_engine.dialect,
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    operations.create_table("aih_dm_migration", Column("id", Integer, Identity(), primary_key=True))
    operations.add_column("aih_dm_migration", Column("note", String(64)))
    operations.drop_column("aih_dm_migration", "note")
    operations.drop_table("aih_dm_migration")
    assert "CREATE TABLE" in output.getvalue().upper()
    assert "DROP TABLE" in output.getvalue().upper()


@pytest.fixture(scope="module")
def live_engine():
    # 只读取专用测试连接，绝不自动使用业务 DATABASE_URL 或本地 .env。
    engine = create_db_engine(os.environ["DM_TEST_DATABASE_URL"])
    try:
        with engine.connect() as connection:
            assert connection.scalar(select(literal(1))) == 1
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def live_table(live_engine):
    table = make_probe_table(f"aih_dm_{uuid4().hex[:12]}")
    # 不使用 drop_all；只清理本次成功创建的随机验证表。
    with live_engine.begin() as connection:
        connection.execute(CreateTable(table))
    try:
        # 表创建成功后再建索引，索引失败也会进入清理路径。
        for index in table.indexes:
            index.create(live_engine)
        yield table
    finally:
        table.drop(live_engine, checkfirst=False)


@pytest.mark.dm_live
def test_live_types_and_generated_primary_key(live_engine, live_table):
    timestamp = datetime(2026, 9, 11, 12, 34, 56, 123456)
    body = "数据集与知识库兼容性验证。" * 1000
    payload = b"\x00\x01\xff\x7f"
    with live_engine.begin() as connection:
        result = connection.execute(
            live_table.insert().values(
                code="types",
                title="中文名称 O'Reilly",
                body=body,
                amount=Decimal("123456789.1234"),
                created_at=timestamp,
                active=True,
                attachment=payload,
            )
        )
        record_id = result.inserted_primary_key[0]
        assert isinstance(record_id, int) and record_id > 0
    with live_engine.connect() as connection:
        row = connection.execute(select(live_table).where(live_table.c.id == record_id)).one()
        assert row.title == "中文名称 O'Reilly"
        assert row.body == body
        assert row.amount == Decimal("123456789.1234")
        assert row.created_at == timestamp
        assert row.active is True
        assert row.attachment == payload


@pytest.mark.dm_live
def test_live_orm_commit_rollback_update_and_delete(live_engine, live_table):
    class Record:
        pass

    mapper = registry()
    mapper.map_imperatively(Record, live_table)
    try:
        dependency = get_session(live_engine)
        session = next(dependency)
        try:
            record = Record()
            record.code, record.title = "orm", "初始名称"
            session.add(record)
            session.commit()
            record_id = record.id
            assert isinstance(record_id, int)
        finally:
            dependency.close()

        dependency = get_session(live_engine)
        session = next(dependency)
        try:
            record = session.get(Record, record_id)
            record.title = "已更新"
            session.commit()
            record.title = "未提交的修改"
            session.flush()
        finally:
            # 复用项目会话清理逻辑，验证连接归还前确实回滚未提交的 SQL。
            dependency.close()

        dependency = get_session(live_engine)
        session = next(dependency)
        try:
            record = session.get(Record, record_id)
            assert record.title == "已更新"
            session.delete(record)
            session.commit()
        finally:
            dependency.close()
        with live_engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(live_table)) == 0
    finally:
        mapper.dispose()


@pytest.mark.dm_live
def test_live_unique_constraint_and_transaction_rollback(live_engine, live_table):
    with live_engine.begin() as connection:
        connection.execute(live_table.insert().values(code="same", title="已提交"))
    with pytest.raises(IntegrityError), live_engine.begin() as connection:
        connection.execute(live_table.insert().values(code="other", title="应回滚"))
        connection.execute(live_table.insert().values(code="same", title="重复值"))
    with live_engine.connect() as connection:
        assert connection.scalars(select(live_table.c.code)).all() == ["same"]


@pytest.mark.dm_live
def test_live_pagination_and_nullable_columns(live_engine, live_table):
    with live_engine.begin() as connection:
        connection.execute(
            live_table.insert(),
            [{"code": f"item_{number}", "title": f"条目 {number}"} for number in range(6)],
        )
    with live_engine.connect() as connection:
        rows = connection.execute(
            select(live_table).order_by(live_table.c.code).offset(2).limit(2)
        ).all()
        assert [row.code for row in rows] == ["item_2", "item_3"]
        assert all(row.body is None and row.amount is None for row in rows)


@pytest.mark.dm_live
@pytest.mark.parametrize("kind", ["columns", "primary_key", "unique_constraints", "indexes"])
def test_live_reflection(live_engine, live_table, kind):
    # 分别检查，避免列反射的权限错误掩盖主键、约束和索引的实际结果。
    inspector = inspect(live_engine)
    if kind == "columns":
        columns = {column["name"].lower() for column in inspector.get_columns(live_table.name)}
        assert columns == set(live_table.c.keys())
    elif kind == "primary_key":
        constraint = inspector.get_pk_constraint(live_table.name)
        assert [name.lower() for name in constraint["constrained_columns"]] == ["id"]
    elif kind == "unique_constraints":
        constraints = inspector.get_unique_constraints(live_table.name)
        assert any(
            [name.lower() for name in item["column_names"]] == ["code"] for item in constraints
        )
    else:
        indexes = inspector.get_indexes(live_table.name)
        assert any([name.lower() for name in item["column_names"]] == ["title"] for item in indexes)


@pytest.mark.dm_live
def test_live_extended_unicode_round_trip(live_engine, live_table):
    # AI 内容可能包含表情和扩展汉字，不能只用 GBK 可表示的常用中文验收编码。
    title = "模型发布 \U0001f680 扩展汉字 \U00020000"
    body = title * 1000
    with live_engine.begin() as connection:
        connection.execute(live_table.insert().values(code="unicode", title=title, body=body))
    with live_engine.connect() as connection:
        row = connection.execute(select(live_table.c.title, live_table.c.body)).one()
        assert row.title == title
        assert row.body == body


@pytest.mark.dm_live
def test_live_json_round_trip(live_engine):
    table = Table(
        f"aih_dm_j_{uuid4().hex[:12]}",
        MetaData(naming_convention=Base.metadata.naming_convention),
        Column("id", Integer, primary_key=True),
        Column("payload", JSON),
    )
    # 单独建表；原生 JSON 不兼容时不影响其他常规类型的检查。
    with live_engine.begin() as connection:
        connection.execute(CreateTable(table))
    try:
        payload = {"name": "数据集", "versions": [1, 2], "active": True, "note": None}
        with live_engine.begin() as connection:
            connection.execute(table.insert().values(id=1, payload=payload))
        with live_engine.connect() as connection:
            assert connection.scalar(select(table.c.payload).where(table.c.id == 1)) == payload
    finally:
        table.drop(live_engine, checkfirst=False)


@pytest.mark.dm_live
def test_live_foreign_key_and_join(live_engine, live_table):
    child = Table(
        f"aih_dm_f_{uuid4().hex[:12]}",
        live_table.metadata,
        Column("id", Integer, primary_key=True),
        Column("parent_id", ForeignKey(live_table.c.id), nullable=False),
    )
    with live_engine.begin() as connection:
        connection.execute(CreateTable(child))
    try:
        with live_engine.begin() as connection:
            parent_id = connection.execute(
                live_table.insert().values(code="parent", title="资源目录")
            ).inserted_primary_key[0]
            connection.execute(child.insert().values(id=1, parent_id=parent_id))
        with pytest.raises(IntegrityError), live_engine.begin() as connection:
            connection.execute(child.insert().values(id=2, parent_id=parent_id + 999))
        with pytest.raises(IntegrityError), live_engine.begin() as connection:
            connection.execute(live_table.delete().where(live_table.c.id == parent_id))
        with live_engine.connect() as connection:
            statement = select(live_table.c.title).select_from(child.join(live_table))
            assert connection.scalars(statement).all() == ["资源目录"]
    finally:
        # 先清理引用表，随后 live_table fixture 才能删除父表。
        child.drop(live_engine, checkfirst=False)


@pytest.mark.dm_live
def test_live_fastapi_readiness_and_engine_lifecycle():
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["DM_TEST_DATABASE_URL"],
        cors_origins=[],
    )
    application = create_app(settings)
    with TestClient(application) as client:
        for _ in range(3):
            response = client.get("/api/v1/health/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ready"}
        assert client.get("/api/v1/health/live").status_code == 200
    assert application.state.database_engine is None


@pytest.mark.dm_live
def test_live_alembic_autogeneration(live_engine, live_table):
    with live_engine.connect() as connection:
        # 限定为本次验证表，避免把测试库中其他表当成待删除的迁移对象。
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "include_name": lambda name, kind, parents: (
                    name == live_table.name if kind == "table" else True
                ),
            },
        )
        assert compare_metadata(context, live_table.metadata) == []


@pytest.mark.dm_live
def test_live_alembic_ddl_round_trip(live_engine):
    name = f"aih_dm_m_{uuid4().hex[:12]}"
    with live_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        operations.create_table(name, Column("id", Integer, Identity(), primary_key=True))
        try:
            connection.commit()
            operations.add_column(name, Column("note", String(64)))
            connection.commit()
            assert "note" in {
                item["name"].lower() for item in inspect(connection).get_columns(name)
            }
            operations.drop_column(name, "note")
            connection.commit()
            assert "note" not in {
                item["name"].lower() for item in inspect(connection).get_columns(name)
            }
        finally:
            connection.rollback()
            operations.drop_table(name)
            connection.commit()


@pytest.mark.dm_live
def test_live_pool_reuse_and_explicit_invalidation(live_engine):
    for _ in range(3):
        with live_engine.connect() as connection:
            assert connection.scalar(select(literal(1))) == 1
    with live_engine.connect() as connection:
        connection.invalidate()
    with live_engine.connect() as connection:
        assert connection.scalar(select(literal(1))) == 1
