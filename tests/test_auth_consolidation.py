"""四表迁移的数据保留、失败保护及 PostgreSQL 约束验证。"""

import json
import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from legacy_auth_models import (
    AuthEventLog as OldLog,
)
from legacy_auth_models import (
    AuthSession as OldSession,
)
from legacy_auth_models import (
    IdentityRole,
    IdentityState,
    IdentitySyncReceipt,
)
from sqlalchemy import MetaData, Table, create_engine, event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.modules.common.auth.models import AuthSession, OperationLog, User
from app.modules.common.auth.security import digest
from app.modules.common.identity_sync.models import IamSyncReceipt

ROOT = Path(__file__).resolve().parents[1]
OLD = {
    "auth_session",
    "auth_login_request",
    "auth_event_log",
    "iam_identity_state",
    "iam_identity_role",
    "iam_sync_receipt",
}
NEW = {"users", "auth_sessions", "operation_logs", "iam_sync_receipts"}
FEEDBACK = {"feedback_records", "feedback_attachments", "feedback_events"}


def migration_config(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    config.cmd_opts = Namespace(
        x=[
            f"auth_archive_dir={tmp_path / 'archive'}",
            "auth_mock_admin_subjects=mock-admin",
        ]
    )
    return config


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_db(request, tmp_path, monkeypatch):
    schema = admin = None
    if request.param == "postgresql":
        configured = os.environ.get("IDENTITY_SYNC_TEST_DATABASE_URL")
        if not configured:
            pytest.skip("requires an explicit PostgreSQL test connection")
        url = make_url(configured)
        assert url.get_backend_name() == "postgresql"
        schema = "auth_migration_test_" + uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(CreateSchema(schema))
        url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    else:
        url = make_url(f"sqlite:///{tmp_path / 'consolidation.db'}")
    engine = create_engine(url)
    if request.param == "sqlite":

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    config = migration_config(tmp_path)
    try:
        command.upgrade(config, "20260917_01")
        yield engine, config, tmp_path
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
            admin.dispose()


def seed_legacy(engine):
    """覆盖有效/旧入口会话、停用、提前注销、撤权与待确认身份。"""
    timestamp = int(datetime.now(UTC).timestamp())
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE preserved_business (owner_id VARCHAR(200))"))
        connection.execute(text("INSERT INTO preserved_business VALUES ('mock-admin')"))
        feedback = Table("feedback_records", MetaData(), autoload_with=connection)
        connection.execute(
            feedback.insert().values(
                id=str(uuid4()),
                submitter_id="mock-admin",
                submitter_name="原管理员",
                feedback_type="suggestion",
                module_code="common",
                title="保留已有反馈",
                description="认证迁移不能改变反馈归属与内容",
                contact="internal-test",
                status="processing",
                version=1,
                client_request_id=str(uuid4()),
                payload_hash="f" * 64,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        for subject, status, version in (
            ("mock-admin", "active", 0),
            ("blocked", "disabled", 200),
            ("revoked-admin", "active", 100),
            ("role-only", "active", 0),
        ):
            connection.execute(
                IdentityState.__table__.insert().values(
                    subject=subject,
                    status=status,
                    sync_time=version,
                    updated_at=timestamp - 20,
                )
            )
        for subject, allowed in (("revoked-admin", False), ("role-only", True)):
            connection.execute(
                IdentityRole.__table__.insert().values(
                    subject=subject,
                    role_code="platform_admin",
                    authorized=allowed,
                    sync_time=300,
                    updated_at=timestamp - 10,
                )
            )
        for subject, scope, name, token in (
            ("mock-admin", "platform", "原管理员", "keep"),
            ("mock-admin", "platform", "最新姓名", "newest"),
            ("blocked", "platform", "已停用", "blocked"),
            ("revoked-admin", "user", "撤权用户", "legacy"),
            (None, "slo", None, "marker"),
        ):
            connection.execute(
                OldSession.__table__.insert().values(
                    token_hash=digest(token),
                    ticket_hash=digest("ST-" + token),
                    subject=subject,
                    scope=scope,
                    display_name=name,
                    created_at=timestamp - (5 if token == "newest" else 60),
                    expires_at=timestamp + 600,
                    revoked_at=timestamp if scope == "slo" else None,
                )
            )
        for action, result, subject in (
            ("login", "success", "mock-admin"),
            ("single_logout", "success", None),
            ("login", "invalid_ticket", None),
        ):
            connection.execute(
                OldLog.__table__.insert().values(
                    id=str(uuid4()),
                    subject=subject,
                    scope="platform",
                    event=action,
                    result=result,
                    request_id=str(uuid4()),
                    created_at=timestamp,
                )
            )
        connection.execute(
            IdentitySyncReceipt.__table__.insert().values(
                event_id="old-event",
                payload_hash="a" * 64,
                resource_type="user",
                sync_time=200,
                applied=1,
                ignored=0,
                unchanged=0,
                revoked_sessions=1,
                attempts=2,
                received_at=timestamp,
                last_received_at=timestamp,
            )
        )


def test_conversion_preserves_sessions_status_roles_logs_and_unrelated_data(migration_db):
    engine, config, directory = migration_db
    seed_legacy(engine)
    command.upgrade(config, "head")
    command.check(config)
    assert NEW.issubset(inspect(engine).get_table_names())
    assert FEEDBACK.issubset(inspect(engine).get_table_names())
    assert not OLD.intersection(inspect(engine).get_table_names())
    with Session(engine) as db:
        users = {user.iam_subject: user for user in db.scalars(select(User))}
        assert len(users) == 4
        admin = users["mock-admin"]
        assert admin.role == "admin" and admin.user_sync_time is None
        assert admin.display_name == "最新姓名" and admin.created_at.tzinfo is not None
        assert users["blocked"].status == "disabled" and users["blocked"].user_sync_time == 200
        assert users["revoked-admin"].role == "user"
        assert users["revoked-admin"].role_sync_time == 300
        assert users["role-only"].status == "pending" and users["role-only"].role == "admin"
        sessions = {row.ticket_hash: row for row in db.scalars(select(AuthSession))}
        assert len(sessions) == 5
        assert sessions[digest("ST-keep")].token_hash == digest("keep")
        assert sessions[digest("ST-keep")].user_id == admin.id
        assert sessions[digest("ST-keep")].revoked_at is None
        assert sessions[digest("ST-blocked")].revoked_at is not None
        assert sessions[digest("ST-legacy")].revoked_at is not None
        marker = sessions[digest("ST-marker")]
        assert marker.record_type == "ticket_revocation" and marker.user_id is None
        assert marker.token_hash is None and marker.revoked_at is not None
        logs = db.scalars(select(OperationLog)).all()
        assert len(logs) == 2
        assert {row.actor_type for row in logs} == {"user", "iam"}
        assert all(row.request_id is not None for row in logs)
        assert db.scalar(select(IamSyncReceipt)) is None
        assert db.scalar(text("SELECT owner_id FROM preserved_business")) == "mock-admin"
        assert db.execute(
            text("SELECT submitter_id, title, version FROM feedback_records")
        ).one() == ("mock-admin", "保留已有反馈", 1)
        identities = {key: user.id for key, user in users.items()}
    archives = list((directory / "archive").glob("legacy-auth-*.json"))
    assert len(archives) == 1
    archive = json.loads(archives[0].read_text(encoding="utf-8"))["tables"]
    assert len(archive["auth_event_log"]) == 3 and len(archive["iam_sync_receipt"]) == 1
    command.upgrade(config, "head")
    with Session(engine) as db:
        assert {user.iam_subject: user.id for user in db.scalars(select(User))} == identities
    with pytest.raises(RuntimeError, match="restoring the pre-upgrade"):
        command.downgrade(config, "20260917_01")
    assert NEW.issubset(inspect(engine).get_table_names())


def test_nonempty_migration_requires_archive_before_ddl(migration_db):
    engine, config, _ = migration_db
    seed_legacy(engine)
    config.cmd_opts = Namespace(x=[])
    with pytest.raises(RuntimeError, match="auth_archive_dir"):
        command.upgrade(config, "head")
    assert OLD.issubset(inspect(engine).get_table_names())
    assert not NEW.intersection(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM auth_session")) == 5
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260917_01"


def test_unmapped_roles_stop_before_dropping_data(migration_db):
    engine, config, _ = migration_db
    seed_legacy(engine)
    with engine.begin() as connection:
        connection.execute(
            IdentityRole.__table__.insert().values(
                subject="mock-admin",
                role_code="unmapped",
                authorized=True,
                sync_time=1,
                updated_at=1,
            )
        )
    with pytest.raises(RuntimeError, match="Unmapped legacy IAM role"):
        command.upgrade(config, "head")
    assert OLD.issubset(inspect(engine).get_table_names())
    assert not NEW.intersection(inspect(engine).get_table_names())


def test_postgresql_enforces_identity_immutability_and_business_update_times(migration_db):
    engine, config, _ = migration_db
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL trigger contract")
    command.upgrade(config, "head")
    original = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as db:
        user = User(iam_subject="user-1", created_at=original, updated_at=original)
        db.add(user)
        db.commit()
        key = user.id
        user.user_sync_time = 1
        user.updated_at = datetime.now(UTC)
        db.commit()
        assert user.updated_at == original
        user.display_name = "姓名"
        db.commit()
        assert user.updated_at > original and user.created_at == original
    for column in ("id", "iam_subject"):
        with pytest.raises(IntegrityError), Session(engine) as db:
            user = db.get(User, key)
            setattr(user, column, uuid4() if column == "id" else "replacement")
            db.commit()


def test_new_constraints_protect_session_binding_and_historical_references(migration_db):
    engine, config, _ = migration_db
    command.upgrade(config, "head")
    with Session(engine) as db:
        user = User(iam_subject="bound-user", status="active")
        db.add(user)
        db.commit()
        key = user.id
        session = AuthSession(
            user_id=key,
            token_hash="a" * 64,
            ticket_hash="b" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(session)
        db.commit()
    with pytest.raises(IntegrityError), Session(engine) as db:
        db.delete(db.get(User, key))
        db.commit()
    for overrides in (
        {"user_id": None},
        {"token_hash": "a" * 64},
        {"ticket_hash": "b" * 64},
        {"record_type": "ticket_revocation"},
        {"record_type": "bad"},
        {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ):
        with pytest.raises(IntegrityError), Session(engine) as db:
            values = (
                dict(
                    user_id=key,
                    token_hash="c" * 64,
                    ticket_hash="d" * 64,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                | overrides
            )
            # 用 Core 绕过 Python Enum 校验，直接验证数据库约束。
            table = AuthSession.__table__
            if overrides.get("record_type") == "bad":
                db.execute(
                    text(
                        "INSERT INTO auth_sessions (id, record_type, ticket_hash, expires_at) "
                        "VALUES (:id, 'bad', :ticket, :expires)"
                    ),
                    {
                        "id": str(uuid4()),
                        "ticket": "e" * 64,
                        "expires": datetime.now(UTC) + timedelta(hours=1),
                    },
                )
            else:
                db.execute(table.insert().values(**values))
            db.commit()
