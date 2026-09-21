"""验证已落库的索引范围收缩，确保审批资料保留且非空索引不会被误删。"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.schema import CreateSchema
from test_home_knowledge_schema import document, knowledge_base, review_document
from test_resource_foundation import ROOT, flush_constraints, insert, update, user
from test_resource_foundation import connection as connection
from test_resource_foundation import pytestmark as pytestmark
from test_resource_foundation import schema_connection as schema_connection
from test_resource_support import get_row

PREVIOUS = "20260921_03_home_knowledge"
CURRENT = "20260921_04_knowledge_external"
REMOVED_TABLES = {"knowledge_index_states", "knowledge_index_jobs", "knowledge_document_chunks"}
REMOVED_COLUMNS = {"index_job_id", "processing_status", "processed_at", "error_message"}


@pytest.fixture
def legacy_schema(connection: sa.Connection) -> Iterator[tuple[sa.Connection, Config]]:
    """在可回滚的临时 schema 构造旧版本，避免修改当前业务库。"""
    with connection.begin_nested() as savepoint:
        try:
            name = "knowledge_scope_test_" + uuid4().hex
            connection.execute(CreateSchema(name))
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{name}"')
            config = Config(str(ROOT / "alembic.ini"), ini_section="resource_foundation")
            config.attributes["connection"] = connection
            command.upgrade(config, PREVIOUS)
            yield connection, config
        finally:
            savepoint.rollback()


def test_scope_migration_preserves_document_review_and_file(legacy_schema):
    """有真实审批资料、但未开始本地索引时，可以撤回三表并继续使用文档审核。"""
    connection, config = legacy_schema
    owner = user(connection)
    base = knowledge_base(connection, owner)
    document_id = document(connection, owner, base)
    review_id = review_document(connection, document_id, owner)
    update(connection, "knowledge_documents", document_id, revision=3, processing_status="pending")
    flush_constraints(connection)
    before_document = get_row(connection, "knowledge_documents", document_id)
    before_review = get_row(connection, "knowledge_document_reviews", review_id)
    before_file = get_row(connection, "files", before_document["file_id"])
    command.upgrade(config, CURRENT)
    command.upgrade(config, CURRENT)
    assert not REMOVED_TABLES.intersection(sa.inspect(connection).get_table_names())
    assert get_row(connection, "knowledge_documents", document_id) == {
        name: value for name, value in before_document.items() if name not in REMOVED_COLUMNS
    }
    assert get_row(connection, "knowledge_document_reviews", review_id) == before_review
    assert get_row(connection, "files", before_document["file_id"]) == before_file
    next_document = document(connection, owner, base)
    review_document(connection, next_document, owner, "rejected")
    assert (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        == CURRENT
    )


@pytest.mark.parametrize("existing_data", ["configuration", "document_result"])
def test_scope_migration_stops_before_dropping_index_data(legacy_schema, existing_data):
    """如索引已被使用则整次迁移失败回滚，不能静默删除配置或处理结果。"""
    connection, config = legacy_schema
    owner = user(connection)
    base = knowledge_base(connection, owner)
    if existing_data == "configuration":
        identifier = insert(connection, "knowledge_index_states", knowledge_base_id=base)
        table = "knowledge_index_states"
    else:
        identifier = document(connection, owner, base)
        update(connection, "knowledge_documents", identifier, revision=2, error_message="已有结果")
        table = "knowledge_documents"
    flush_constraints(connection)
    before = get_row(connection, table, identifier)
    with pytest.raises(sa.exc.IntegrityError, match="archive it before removal"):
        with connection.begin_nested():
            command.upgrade(config, CURRENT)
    assert REMOVED_TABLES.issubset(sa.inspect(connection).get_table_names())
    assert (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
        == PREVIOUS
    )
    assert get_row(connection, table, identifier) == before
