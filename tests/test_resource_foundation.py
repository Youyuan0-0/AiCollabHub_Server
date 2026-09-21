"""在专门测试库的隔离事务中验证新基线；所有测试数据和 schema 最终回滚。"""

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from app.modules.common.auth.models import AuthSession, OperationLog, User
from app.modules.common.identity_sync.models import IamSyncReceipt
from app.modules.common.system_logs import SystemLog

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    ("dataset", "datasets", "dataset"),
    ("model", "models", "model"),
    ("application", "applications", "application"),
    ("knowledge", "knowledge_bases", "knowledge_base"),
    ("tool", "ai_tools", "ai_tool"),
]
pytestmark = pytest.mark.skipif(
    not os.environ.get("RESOURCE_FOUNDATION_TEST_DATABASE_URL"),
    reason="Set RESOURCE_FOUNDATION_TEST_DATABASE_URL to an isolated PostgreSQL test database",
)


@pytest.fixture(scope="module")
def schema_connection():
    """仅使用显式测试连接，临时 schema 与全部测试写入在退出时一起回滚。"""
    url = sa.engine.make_url(os.environ["RESOURCE_FOUNDATION_TEST_DATABASE_URL"])
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        pytest.fail("Resource foundation tests require PostgreSQL with psycopg")
    # 不读取项目 .env，也不将普通业务连接隐式作为测试目标。
    engine = sa.create_engine(
        url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                schema = "foundation_test_" + uuid4().hex
                connection.execute(CreateSchema(schema))
                # 不回落到 public，避免 Alembic 误认正式表的版本号并跳过隔离库基线。
                connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
                connection.exec_driver_sql("SET LOCAL lock_timeout='5s'")
                connection.exec_driver_sql("SET LOCAL statement_timeout='60s'")
                config = Config(str(ROOT / "alembic.ini"), ini_section="resource_foundation")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                command.upgrade(config, "head")
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.fixture
def connection(schema_connection):
    with schema_connection.begin_nested() as savepoint:
        schema_connection.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")
        yield schema_connection
        savepoint.rollback()


def insert(connection, table_name, **values):
    table = sa.table(table_name, *(sa.column(key) for key in values))
    return connection.execute(
        table.insert().values(**values).returning(sa.column("id"))
    ).scalar_one()


def update(connection, table_name, row_id, **values):
    table = sa.table(table_name, sa.column("id"), *(sa.column(key) for key in values))
    connection.execute(table.update().where(table.c.id == row_id).values(**values))


def flush_constraints(connection):
    connection.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
    connection.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")


@contextmanager
def rejected(connection):
    with pytest.raises(IntegrityError), connection.begin_nested():
        yield
        flush_constraints(connection)


def user(connection):
    return insert(
        connection, "users", iam_subject="schema-test-" + uuid4().hex, role="admin", status="active"
    )


def resource(connection, module, owner, **overrides):
    kind, name, _ = module
    values = {
        "name": "资源验证",
        "summary": "测试概览",
        "provider_name": "测试单位",
        "creator_id": owner,
    }
    if kind == "dataset":
        values.update(description_markdown="数据说明", ai_purpose="training")
    elif kind == "knowledge":
        values.update(knowledge_type="file")
    elif kind in {"tool", "application"}:
        values.update(category="test_category")
    if kind == "tool":
        values.update(source_type="self_developed")
    return insert(connection, name, **(values | overrides))


def pending(connection, module, owner, **overrides):
    _, _, prefix = module
    values = {
        "applicant_id": owner,
        "version": "1.0",
        "review_status": "pending",
        "submission_no": 1,
        "submitted_at": datetime.now(UTC),
        "data": Jsonb({"name": "资源验证"}),
    }
    return insert(connection, f"{prefix}_publications", **(values | overrides))


def review(connection, module, publication_id, owner, decision="approved", **overrides):
    kind, _, prefix = module
    row = (
        connection.execute(
            sa.text(f"SELECT * FROM {prefix}_publications WHERE id=:id"), {"id": publication_id}
        )
        .mappings()
        .one()
    )
    values = {
        "resource_type": kind,
        "publication_id": publication_id,
        "submission_no": row["submission_no"],
        "reviewer_id": owner,
        "decision": decision,
        "comment": "请补充资料" if decision == "rejected" else None,
        "submitted_at": row["submitted_at"],
        "submission_snapshot": Jsonb(
            {"version": row["version"], "versionNotes": row["version_notes"], "data": row["data"]}
        ),
    }
    review_id = insert(connection, "resource_reviews", **(values | overrides))
    update(
        connection,
        f"{prefix}_publications",
        publication_id,
        review_status=decision,
        revision=row["revision"] + 1,
    )
    return review_id


def published(connection, module, owner):
    _, name, prefix = module
    publication_id = pending(connection, module, owner)
    review_id = review(connection, module, publication_id, owner)
    resource_id = resource(connection, module, owner)
    now = datetime.now(UTC)
    version_id = insert(
        connection,
        f"{prefix}_versions",
        **{f"{prefix}_id": resource_id},
        publication_id=publication_id,
        version="1.0",
        snapshot=Jsonb({"name": "资源验证"}),
        publisher_id=owner,
        published_at=now,
    )
    update(
        connection,
        f"{prefix}_publications",
        publication_id,
        **{f"{prefix}_id": resource_id},
        published_version_id=version_id,
        published_at=now,
        revision=3,
    )
    update(connection, name, resource_id, current_version_id=version_id)
    flush_constraints(connection)
    return resource_id, publication_id, version_id, review_id


def test_exact_tables_and_chinese_comments(connection):
    actual = set(sa.inspect(connection).get_table_names())
    expected = {
        "alembic_version",
        "users",
        "auth_sessions",
        "iam_sync_receipts",
        "operation_logs",
        "system_logs",
        "resource_favorites",
        "resource_follows",
        "resource_ratings",
        "resource_downloads",
        "resource_reviews",
        "files",
        "feedback_records",
        "feedback_events",
        "feedback_attachments",
        "application_dependencies",
        "module_configurations",
        "resource_visibility_policies",
        "resource_visibility_targets",
        "resource_file_references",
        "file_previews",
        "conversations",
        "conversation_messages",
        "conversation_message_attachments",
        "home_recommendations",
        "home_question_suggestions",
        "announcements",
        "knowledge_documents",
        "knowledge_document_reviews",
    }
    for _, name, prefix in MODULES:
        expected.update({name, f"{prefix}_publications", f"{prefix}_versions"})
    assert actual == expected
    missing = connection.execute(
        sa.text("""
        SELECT c.relname, a.attname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        WHERE n.nspname=current_schema() AND c.relkind='r' AND c.relname<>'alembic_version'
          AND (col_description(c.oid,a.attnum) IS NULL OR obj_description(c.oid) IS NULL)
    """)
    ).all()
    assert missing == []


@pytest.mark.parametrize("module", MODULES)
def test_all_modules_publish_and_interact(connection, module):
    owner = user(connection)
    resource_id, _, version_id, _ = published(connection, module, owner)
    assert (
        connection.execute(
            sa.text(f"SELECT status FROM {module[1]} WHERE id=:id"), {"id": resource_id}
        ).scalar_one()
        == "disabled"
    )
    update(connection, module[1], resource_id, status="enabled")
    values = {"resource_type": module[0], "resource_id": resource_id, "user_id": owner}
    insert(connection, "resource_favorites", **values)
    insert(connection, "resource_follows", **values)
    insert(connection, "resource_ratings", **values, rating=5)
    insert(
        connection,
        "resource_downloads",
        **values,
        version_id=version_id,
        idempotency_key="download",
        status="link_issued",
    )
    flush_constraints(connection)


@pytest.mark.parametrize("table", ["resource_favorites", "resource_follows", "resource_ratings"])
def test_duplicate_interactions_are_rejected(connection, table):
    owner = user(connection)
    target = resource(connection, MODULES[0], owner)
    values = {"resource_type": "dataset", "resource_id": target, "user_id": owner}
    if table == "resource_ratings":
        values["rating"] = 4
    insert(connection, table, **values)
    with rejected(connection):
        insert(connection, table, **values)


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_range(connection, rating):
    owner = user(connection)
    target = resource(connection, MODULES[0], owner)
    with rejected(connection):
        insert(
            connection,
            "resource_ratings",
            resource_type="dataset",
            resource_id=target,
            user_id=owner,
            rating=rating,
        )


def test_polymorphic_reference_and_resource_deletion(connection):
    owner = user(connection)
    target = resource(connection, MODULES[0], owner)
    with rejected(connection):
        insert(
            connection,
            "resource_favorites",
            resource_type="model",
            resource_id=target,
            user_id=owner,
        )
    insert(
        connection, "resource_favorites", resource_type="dataset", resource_id=target, user_id=owner
    )
    with rejected(connection):
        connection.execute(sa.text("DELETE FROM datasets WHERE id=:id"), {"id": target})
    with rejected(connection):
        connection.execute(
            sa.text("UPDATE datasets SET id=:new WHERE id=:id"), {"id": target, "new": uuid4()}
        )


def test_current_and_download_versions_must_belong_to_resource(connection):
    owner = user(connection)
    _, _, version_id, _ = published(connection, MODULES[0], owner)
    other = resource(connection, MODULES[0], owner)
    with rejected(connection):
        update(connection, "datasets", other, current_version_id=version_id)
    with rejected(connection):
        insert(
            connection,
            "resource_downloads",
            resource_type="dataset",
            resource_id=other,
            user_id=owner,
            version_id=version_id,
            idempotency_key="wrong-version",
        )


def test_download_idempotency_and_completion(connection):
    owner = user(connection)
    target = resource(connection, MODULES[0], owner)
    values = {
        "resource_type": "dataset",
        "resource_id": target,
        "user_id": owner,
        "idempotency_key": "same-request",
    }
    download = insert(connection, "resource_downloads", **values, status="link_issued")
    with rejected(connection):
        insert(connection, "resource_downloads", **values)
    with rejected(connection):
        update(connection, "resource_downloads", download, status="completed")
    update(
        connection,
        "resource_downloads",
        download,
        status="completed",
        completed_at=datetime.now(UTC),
    )


def test_approval_requires_matching_review(connection):
    owner = user(connection)
    publication_id = pending(connection, MODULES[0], owner)
    with rejected(connection):
        update(
            connection, "dataset_publications", publication_id, review_status="approved", revision=2
        )


def test_review_snapshot_and_atomic_outcome(connection):
    owner = user(connection)
    publication_id = pending(connection, MODULES[0], owner)
    with rejected(connection):
        review(connection, MODULES[0], publication_id, owner, submission_snapshot=Jsonb({}))
    row = (
        connection.execute(
            sa.text("SELECT * FROM dataset_publications WHERE id=:id"), {"id": publication_id}
        )
        .mappings()
        .one()
    )
    with rejected(connection):
        insert(
            connection,
            "resource_reviews",
            resource_type="dataset",
            publication_id=publication_id,
            submission_no=1,
            reviewer_id=owner,
            decision="approved",
            submitted_at=row["submitted_at"],
            submission_snapshot=Jsonb(
                {"version": "1.0", "versionNotes": None, "data": row["data"]}
            ),
        )


def test_rejected_submission_can_be_edited_and_resubmitted(connection):
    owner = user(connection)
    publication_id = pending(connection, MODULES[0], owner)
    review(connection, MODULES[0], publication_id, owner, "rejected")
    flush_constraints(connection)
    update(
        connection,
        "dataset_publications",
        publication_id,
        data=Jsonb({"name": "修订后"}),
        revision=3,
    )
    update(
        connection,
        "dataset_publications",
        publication_id,
        review_status="pending",
        submission_no=2,
        submitted_at=datetime.now(UTC),
        revision=4,
    )
    review(connection, MODULES[0], publication_id, owner)
    flush_constraints(connection)
    rows = connection.execute(
        sa.text(
            "SELECT submission_no, decision, submission_snapshot FROM resource_reviews "
            "WHERE publication_id=:id ORDER BY submission_no"
        ),
        {"id": publication_id},
    ).all()
    assert [(r[0], r[1], r[2]["data"]["name"]) for r in rows] == [
        (1, "rejected", "资源验证"),
        (2, "approved", "修订后"),
    ]
    with rejected(connection):
        connection.execute(
            sa.text("DELETE FROM dataset_publications WHERE id=:id"), {"id": publication_id}
        )


def test_pending_content_and_revision_are_protected(connection):
    owner = user(connection)
    publication_id = pending(connection, MODULES[0], owner)
    with rejected(connection):
        update(
            connection,
            "dataset_publications",
            publication_id,
            data=Jsonb({"name": "修改"}),
            revision=2,
        )
    with rejected(connection):
        update(connection, "dataset_publications", publication_id, revision=1)


@pytest.mark.parametrize("table,position", [("dataset_versions", 2), ("resource_reviews", 3)])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_published_history_cannot_be_overwritten(connection, table, position, operation):
    record = published(connection, MODULES[0], user(connection))[position]
    sql = (
        f"UPDATE {table} SET id=id WHERE id=:id"
        if operation == "UPDATE"
        else f"DELETE FROM {table} WHERE id=:id"
    )
    with rejected(connection):
        connection.execute(sa.text(sql), {"id": record})


def test_publication_generates_only_one_version(connection):
    owner = user(connection)
    target, publication_id, _, _ = published(connection, MODULES[0], owner)
    with rejected(connection):
        insert(
            connection,
            "dataset_versions",
            dataset_id=target,
            publication_id=publication_id,
            version="2.0",
            publisher_id=owner,
            snapshot=Jsonb({}),
        )


@pytest.mark.parametrize("tags", [{"tag": "x"}, [1], [""], ["x", "x"]])
def test_tag_shape_and_uniqueness(connection, tags):
    with rejected(connection):
        resource(connection, MODULES[0], user(connection), tags=Jsonb(tags))


def test_enable_requires_a_version(connection):
    with rejected(connection):
        resource(connection, MODULES[0], user(connection), status="enabled")


def test_existing_identity_and_log_orm_can_write(connection):
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        account = User(iam_subject="orm-" + uuid4().hex, status="active", role="admin")
        session.add(account)
        session.flush()
        session.add_all(
            [
                AuthSession(
                    user_id=account.id,
                    token_hash="a" * 64,
                    ticket_hash="b" * 64,
                    expires_at=datetime.now(UTC) + timedelta(hours=8),
                ),
                IamSyncReceipt(
                    payload_hash="c" * 64, resource_type="user", action="push", sync_time=1
                ),
                OperationLog(
                    actor_type="user",
                    user_id=account.id,
                    action="login",
                    target_type="session",
                    data={},
                ),
                SystemLog(
                    level="info",
                    source="test",
                    event="schema_verified",
                    message="结构验证",
                    details={},
                ),
            ]
        )
        session.commit()
    flush_constraints(connection)


def test_invalid_session_and_log_level_are_rejected(connection):
    with rejected(connection):
        insert(
            connection,
            "auth_sessions",
            ticket_hash="e" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    with rejected(connection):
        insert(
            connection, "system_logs", level="debug", source="test", event="test", message="test"
        )


def feedback(connection, owner, *, file_ids=(), **overrides):
    """以真实提交事务顺序建立问题、附件和首次提交事件。"""
    request_id = uuid4()
    feedback_id = insert(
        connection,
        "feedback_records",
        **(
            {
                "submitter_id": owner,
                "feedback_type": "function_error",
                "module_code": "datasets",
                "title": "无法查看资料",
                "description": "点击资源后页面未显示资料。",
                "contact_info": "测试联系方式",
                "idempotency_key": request_id,
            }
            | overrides
        ),
    )
    for position, file_id in enumerate(file_ids, start=1):
        insert(
            connection,
            "feedback_attachments",
            feedback_id=feedback_id,
            file_id=file_id,
            position=position,
        )
    insert(
        connection,
        "feedback_events",
        feedback_id=feedback_id,
        actor_id=owner,
        action="submitted",
        content="",
        to_status="processing",
        resulting_revision=1,
        idempotency_key=overrides.get("idempotency_key", request_id),
    )
    flush_constraints(connection)
    return feedback_id


def process_feedback(connection, feedback_id, owner, action, **overrides):
    """原子更新反馈并写入事件，供状态一致性与重复处理测试。"""
    row = (
        connection.execute(
            sa.text("SELECT * FROM feedback_records WHERE id=:id"), {"id": feedback_id}
        )
        .mappings()
        .one()
    )
    status = {"resolved": "resolved", "reopened": "processing"}.get(action, row["status"])
    moment = datetime.now(UTC)
    resolved_at = moment if action == "resolved" else row["resolved_at"]
    if action == "reopened":
        resolved_at = None
    update(
        connection,
        "feedback_records",
        feedback_id,
        status=status,
        revision=row["revision"] + 1,
        resolved_at=resolved_at,
    )
    event_id = insert(
        connection,
        "feedback_events",
        **(
            {
                "feedback_id": feedback_id,
                "actor_id": owner,
                "action": action,
                "content": "已核对并处理",
                "from_status": row["status"],
                "to_status": status,
                "resulting_revision": row["revision"] + 1,
                "idempotency_key": uuid4(),
                "created_at": moment,
            }
            | overrides
        ),
    )
    flush_constraints(connection)
    return event_id


def ready_file(connection, owner, **overrides):
    """登记一个已完成上传的文件元数据，不向 MinIO 上传测试对象。"""
    return insert(
        connection,
        "files",
        **(
            {
                "uploader_id": owner,
                "file_name": "反馈截图.png",
                "size_bytes": 128,
                "bucket": "test-files",
                "object_key": uuid4().hex,
                "status": "ready",
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
            | overrides
        ),
    )


def application_with_dependencies(connection, owner, targets, *, rows=None, snapshot=None):
    """从已审核的同一依赖清单生成版本快照和结构化关联，最后统一检查。"""
    module = MODULES[2]
    details = {"name": "应用关联验证", "dependencies": targets}
    publication_id = pending(connection, module, owner, data=Jsonb(details))
    review(connection, module, publication_id, owner)
    application_id = resource(connection, module, owner)
    moment = datetime.now(UTC)
    version_id = insert(
        connection,
        "application_versions",
        application_id=application_id,
        publication_id=publication_id,
        version="1.0",
        publisher_id=owner,
        snapshot=Jsonb(details if snapshot is None else snapshot),
        published_at=moment,
    )
    update(
        connection,
        "application_publications",
        publication_id,
        application_id=application_id,
        published_version_id=version_id,
        published_at=moment,
        revision=3,
    )
    update(connection, "applications", application_id, current_version_id=version_id)
    for position, target in enumerate(targets if rows is None else rows, start=1):
        insert(
            connection,
            "application_dependencies",
            version_id=version_id,
            resource_type=target["kind"],
            resource_id=target["resourceId"],
            resource_version_id=target["resourceVersionId"],
            position=position,
        )
    flush_constraints(connection)
    return version_id


def test_feedback_processing_history_is_atomic_and_append_only(connection):
    owner = user(connection)
    record_id = feedback(connection, owner)
    process_feedback(connection, record_id, owner, "replied")
    event_id = process_feedback(connection, record_id, owner, "resolved")
    resolved_at = connection.execute(
        sa.text("SELECT resolved_at FROM feedback_records WHERE id=:id"), {"id": record_id}
    ).scalar_one()
    process_feedback(connection, record_id, owner, "replied")
    assert (
        connection.execute(
            sa.text("SELECT resolved_at FROM feedback_records WHERE id=:id"), {"id": record_id}
        ).scalar_one()
        == resolved_at
    )
    process_feedback(connection, record_id, owner, "reopened")
    current = connection.execute(
        sa.text("SELECT status, revision, resolved_at FROM feedback_records WHERE id=:id"),
        {"id": record_id},
    ).one()
    assert tuple(current) == ("processing", 5, None)
    assert connection.execute(
        sa.text(
            "SELECT action FROM feedback_events WHERE feedback_id=:id ORDER BY resulting_revision"
        ),
        {"id": record_id},
    ).scalars().all() == ["submitted", "replied", "resolved", "replied", "reopened"]
    with rejected(connection):
        update(connection, "feedback_events", event_id, content="覆盖历史")
    with rejected(connection):
        connection.execute(sa.text("DELETE FROM feedback_events WHERE id=:id"), {"id": event_id})
    with rejected(connection):
        update(connection, "feedback_records", record_id, title="修改原始问题", revision=6)
    with rejected(connection):
        update(connection, "feedback_records", record_id, revision=6)


@pytest.mark.parametrize(
    "values",
    [
        {"feedback_type": "unknown"},
        {"feedback_type": "other"},
        {"type_other": "多余说明"},
        {"module_code": "other"},
        {"module_other": "多余模块"},
        {"title": "   "},
        {"description": "x" * 5001},
        {"contact_info": " "},
        {"revision": 2},
    ],
)
def test_feedback_rejects_invalid_submission(connection, values):
    with rejected(connection):
        feedback(connection, user(connection), **values)


def test_feedback_idempotency_and_stale_processing(connection):
    owner = user(connection)
    request_id = uuid4()
    record_id = feedback(connection, owner, idempotency_key=request_id)
    with rejected(connection):
        feedback(connection, owner, idempotency_key=request_id)
    feedback(connection, user(connection), idempotency_key=request_id)
    processing_key = uuid4()
    process_feedback(connection, record_id, owner, "replied", idempotency_key=processing_key)
    stale = connection.execute(
        sa.text("UPDATE feedback_records SET revision=revision+1 WHERE id=:id AND revision=1"),
        {"id": record_id},
    )
    assert stale.rowcount == 0
    with rejected(connection):
        process_feedback(connection, record_id, owner, "replied", idempotency_key=processing_key)
    with rejected(connection):
        process_feedback(
            connection, record_id, owner, "replied", from_status="resolved", to_status="resolved"
        )


def test_feedback_requires_submission_event_and_matching_owner(connection):
    owner = user(connection)
    with rejected(connection):
        insert(
            connection,
            "feedback_records",
            submitter_id=owner,
            feedback_type="function_error",
            module_code="home",
            title="问题",
            description="原始描述",
            contact_info="联系方式",
            idempotency_key=uuid4(),
        )
    with rejected(connection):
        feedback(connection, owner, submitter_id=user(connection))


def test_feedback_attachment_freezes_input_and_protects_file(connection):
    owner = user(connection)
    file_id = ready_file(connection, owner)
    record_id = feedback(connection, owner, file_ids=[file_id])
    assert (
        connection.execute(
            sa.text("SELECT expires_at FROM files WHERE id=:id"), {"id": file_id}
        ).scalar_one()
        is None
    )
    with rejected(connection):
        insert(
            connection,
            "feedback_attachments",
            feedback_id=record_id,
            file_id=ready_file(connection, owner),
            position=2,
        )
    with rejected(connection):
        connection.execute(
            sa.text("DELETE FROM feedback_attachments WHERE feedback_id=:id"), {"id": record_id}
        )
    for values in (
        {"status": "deleting"},
        {"object_key": "replacement"},
        {"expires_at": datetime.now(UTC)},
    ):
        with rejected(connection):
            update(connection, "files", file_id, **values)
    with rejected(connection):
        connection.execute(sa.text("DELETE FROM files WHERE id=:id"), {"id": file_id})


@pytest.mark.parametrize(
    "values",
    [
        {"status": "uploading"},
        {"status": "failed"},
        {"status": "deleting"},
        {"expires_at": datetime.now(UTC) - timedelta(hours=1)},
    ],
)
def test_feedback_rejects_unusable_attachment(connection, values):
    owner = user(connection)
    file_id = ready_file(connection, owner, **values)
    with rejected(connection):
        feedback(connection, owner, file_ids=[file_id])


def test_feedback_attachment_limits_and_rollback(connection):
    owner = user(connection)
    file_ids = [ready_file(connection, owner) for _ in range(4)]
    with rejected(connection):
        feedback(connection, owner, file_ids=file_ids)
    with rejected(connection):
        feedback(connection, owner, file_ids=[file_ids[0], file_ids[0]])
    assert connection.execute(sa.text("SELECT count(*) FROM feedback_records")).scalar_one() == 0
    assert (
        connection.execute(
            sa.text("SELECT count(*) FROM files WHERE expires_at IS NULL")
        ).scalar_one()
        == 0
    )
    feedback(connection, owner, file_ids=file_ids[:3])


@pytest.mark.parametrize(
    "values",
    [
        {"size_bytes": -1},
        {"size_bytes": None},
        {"content_hash": "wrong"},
        {"file_name": "../file.png"},
        {"file_name": "folder\\file.png"},
        {"status": "unknown"},
    ],
)
def test_invalid_file_metadata_is_rejected(connection, values):
    with rejected(connection):
        ready_file(connection, user(connection), **values)


def test_file_download_reference_and_lifecycle(connection):
    owner = user(connection)
    resource_id = resource(connection, MODULES[0], owner)
    values = {"resource_type": "dataset", "resource_id": resource_id, "user_id": owner}
    with rejected(connection):
        insert(
            connection, "resource_downloads", **values, file_id=uuid4(), idempotency_key="missing"
        )
    file_id = ready_file(connection, owner)
    insert(connection, "resource_downloads", **values, file_id=file_id, idempotency_key="download")
    with rejected(connection):
        update(connection, "files", file_id, status="deleting")
    other = ready_file(connection, owner)
    with rejected(connection):
        update(connection, "files", other, status="deleted")
    update(connection, "files", other, status="deleting")
    update(connection, "files", other, status="deleted")
    with rejected(connection):
        update(connection, "files", other, status="ready")


@pytest.mark.parametrize("module", [MODULES[0], MODULES[1], MODULES[3], MODULES[4]])
def test_application_dependencies_pin_resource_versions(connection, module):
    owner = user(connection)
    target, _, target_version, _ = published(connection, module, owner)
    dependencies = [
        {"kind": module[0], "resourceId": str(target), "resourceVersionId": str(target_version)}
    ]
    version_id = application_with_dependencies(connection, owner, dependencies)
    rows = connection.execute(
        sa.text(
            "SELECT id,resource_id,resource_version_id FROM application_dependencies "
            "WHERE version_id=:id"
        ),
        {"id": version_id},
    ).all()
    assert len(rows) == 1 and tuple(rows[0][1:]) == (target, target_version)
    with rejected(connection):
        update(connection, "application_dependencies", rows[0][0], position=2)
    with rejected(connection):
        connection.execute(
            sa.text("DELETE FROM application_dependencies WHERE id=:id"), {"id": rows[0][0]}
        )
    with rejected(connection):
        connection.execute(sa.text(f"DELETE FROM {module[1]} WHERE id=:id"), {"id": target})
    with rejected(connection):
        insert(
            connection,
            "application_dependencies",
            version_id=version_id,
            resource_type=module[0],
            resource_id=target,
            resource_version_id=target_version,
            position=2,
        )


def test_application_dependency_rejects_wrong_type_or_version(connection):
    owner = user(connection)
    target, _, target_version, _ = published(connection, MODULES[0], owner)
    other = resource(connection, MODULES[0], owner)
    for kind, resource_id in (("model", target), ("dataset", other), ("application", target)):
        with rejected(connection):
            application_with_dependencies(
                connection,
                owner,
                [
                    {
                        "kind": kind,
                        "resourceId": str(resource_id),
                        "resourceVersionId": str(target_version),
                    }
                ],
            )


def test_application_dependency_snapshot_and_rows_commit_together(connection):
    owner = user(connection)
    target, _, target_version, _ = published(connection, MODULES[0], owner)
    dependencies = [
        {"kind": "dataset", "resourceId": str(target), "resourceVersionId": str(target_version)}
    ]
    with rejected(connection):
        application_with_dependencies(connection, owner, dependencies, rows=[])
    with rejected(connection):
        application_with_dependencies(connection, owner, [], rows=dependencies)
    with rejected(connection):
        application_with_dependencies(
            connection, owner, dependencies, snapshot={"dependencies": []}
        )
    version_id = application_with_dependencies(connection, owner, dependencies)
    assert (
        connection.execute(
            sa.text("SELECT count(*) FROM application_dependencies WHERE version_id=:id"),
            {"id": version_id},
        ).scalar_one()
        == 1
    )
