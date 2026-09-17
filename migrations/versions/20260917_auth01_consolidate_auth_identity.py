"""合并为用户、共享会话、成功操作日志及同步回执四表。

Revision ID: 20260917_auth01
Revises: 20260917_01
Create Date: 2026-09-17 12:58:27.477796
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "20260917_auth01"
down_revision = "20260917_01"
branch_labels = None
depends_on = None


def create_tables() -> None:
    """冻结目标表定义，不依赖后续可能变更的应用 ORM。"""
    op.create_table(
        "iam_sync_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('push', 'recycle')", name=op.f("ck_iam_sync_receipts_action")
        ),
        sa.CheckConstraint(
            "resource_type IN ('user', 'userlinkedrole')",
            name=op.f("ck_iam_sync_receipts_resource_type"),
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_iam_sync_receipts_payload_hash")
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint("sync_time > 0", name=op.f("ck_iam_sync_receipts_sync_time")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_iam_sync_receipts")),
        sa.UniqueConstraint("payload_hash", name=op.f("uq_iam_sync_receipts_payload_hash")),
    )
    op.create_index(
        "ix_iam_sync_receipts_created_at", "iam_sync_receipts", ["created_at"], unique=False
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("iam_subject", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "active",
                "disabled",
                "deleted",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum(
                "user", "admin", name="role", native_enum=False, create_constraint=True, length=16
            ),
            server_default="user",
            nullable=False,
        ),
        sa.Column("user_sync_time", sa.BigInteger(), nullable=True),
        sa.Column("role_sync_time", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "display_name IS NULL OR length(trim(display_name)) > 0",
            name=op.f("ck_users_display_name"),
        ),
        sa.CheckConstraint("length(trim(iam_subject)) > 0", name=op.f("ck_users_iam_subject")),
        sa.CheckConstraint("role_sync_time > 0", name=op.f("ck_users_role_sync_time")),
        sa.CheckConstraint("user_sync_time > 0", name=op.f("ck_users_user_sync_time")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("iam_subject", name=op.f("uq_users_iam_subject")),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "record_type",
            sa.Enum(
                "session",
                "ticket_revocation",
                name="record_type",
                native_enum=False,
                create_constraint=True,
                length=24,
            ),
            server_default="session",
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("ticket_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(record_type = 'session' AND user_id IS NOT NULL AND token_hash IS NOT NULL) OR "
            "(record_type = 'ticket_revocation' AND user_id IS NULL "
            "AND token_hash IS NULL AND revoked_at IS NOT NULL)",
            name=op.f("ck_auth_sessions_record"),
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_auth_sessions_lifetime")),
        sa.CheckConstraint(
            "ticket_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_auth_sessions_ticket_hash")
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_auth_sessions_token_hash")
        ).ddl_if(dialect="postgresql"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("ticket_hash", name=op.f("uq_auth_sessions_ticket_hash")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_index(
        "ix_auth_sessions_active_user_id",
        "auth_sessions",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("record_type = 'session' AND revoked_at IS NULL"),
        sqlite_where=sa.text("record_type = 'session' AND revoked_at IS NULL"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"], unique=False)
    op.create_table(
        "operation_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "actor_type",
            sa.Enum(
                "user",
                "system",
                "iam",
                name="actor_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "action",
            sa.Enum(
                "login",
                "logout",
                "single_logout",
                "create",
                "update",
                "delete",
                "enable",
                "disable",
                "grant",
                "revoke",
                "sync",
                "invoke",
                "upload",
                "download",
                "review",
                "configure",
                name="action",
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column(
            "target_type",
            sa.Enum(
                "user",
                "session",
                "identity_sync",
                "dataset",
                "model",
                "application",
                "knowledge_base",
                "tool",
                "conversation",
                "feedback",
                "file",
                "platform",
                name="target_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("target_id", sa.String(length=200), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=True),
        sa.Column(
            "data",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(actor_type = 'user' AND user_id IS NOT NULL) OR "
            "(actor_type <> 'user' AND user_id IS NULL)",
            name=op.f("ck_operation_logs_actor"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_operation_logs_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'", name=op.f("ck_operation_logs_data")
        ).ddl_if(dialect="postgresql"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operation_logs")),
    )
    op.create_index("ix_operation_logs_created_at", "operation_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_operation_logs_request_id",
        "operation_logs",
        ["request_id"],
        unique=False,
        postgresql_where=sa.text("request_id IS NOT NULL"),
        sqlite_where=sa.text("request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_operation_logs_target_type_target_id_created_at",
        "operation_logs",
        ["target_type", "target_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_operation_logs_user_id_created_at",
        "operation_logs",
        ["user_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )


LEGACY_TABLES = (
    "iam_identity_role",
    "iam_identity_state",
    "auth_session",
    "auth_login_request",
    "auth_event_log",
    "iam_sync_receipt",
)


def epoch(value):
    """旧认证时间为 UTC Unix 秒，与 IAM 的 syncTime 原值分开处理。"""
    return datetime.fromtimestamp(value, UTC) if value is not None else None


def identifier(value):
    """旧事件编号有效时保持 UUID，否则生成新编号；原值保留在归档中。"""
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def read_legacy(connection):
    """锁定旧认证表后读取快照；迁移期间需先停止所有旧版本后端。"""
    if connection.dialect.name == "postgresql":
        connection.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(
            sa.text("LOCK TABLE " + ", ".join(LEGACY_TABLES) + " IN ACCESS EXCLUSIVE MODE")
        )
    metadata = sa.MetaData()
    return {
        name: [
            dict(row)
            for row in connection.execute(
                sa.select(sa.Table(name, metadata, autoload_with=connection))
            ).mappings()
        ]
        for name in LEGACY_TABLES
    }


def archive_legacy(rows, options):
    """删表前写一次性完整快照，保留失败日志、旧回执和未消费登录上下文。"""
    if not any(rows.values()):
        return
    directory = options.get("auth_archive_dir")
    if not directory:
        raise RuntimeError(
            "Nonempty auth tables require -x auth_archive_dir=<private backup directory>; "
            "stop API workers and back up the database before upgrading"
        )
    root = Path(directory).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps(
        {"revision": revision, "parent": down_revision, "tables": rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    filename = root / ("legacy-auth-" + uuid4().hex + ".json")
    # 独占创建，原文只进入部署方指定的私有备份目录，不进入数据库新业务表。
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    if hashlib.sha256(filename.read_bytes()).digest() != hashlib.sha256(body).digest():
        raise RuntimeError("Authentication archive verification failed")


def transform(rows, options):
    """将旧身份映射为稳定 UUID，保留独立来源版本、注销状态和成功事件。"""
    states = {row["subject"]: row for row in rows["iam_identity_state"]}
    roles = {}
    for row in rows["iam_identity_role"]:
        if row["role_code"] != "platform_admin":
            raise RuntimeError("Unmapped legacy IAM role; review role mapping before consolidation")
        roles[row["subject"]] = row
    sessions = sorted(rows["auth_session"], key=lambda row: row["created_at"])
    events = rows["auth_event_log"]
    sessions_by_subject, observed_by_subject = defaultdict(list), defaultdict(list)
    for row in sessions:
        if row["subject"] and row["scope"] != "slo":
            sessions_by_subject[row["subject"]].append(row)
            observed_by_subject[row["subject"]].append(row["created_at"])
    for row in events:
        if row["subject"]:
            observed_by_subject[row["subject"]].append(row["created_at"])
    subjects = set(states) | set(roles)
    subjects.update(row["subject"] for row in sessions + events if row["subject"])
    mock_admins = set(filter(None, options.get("auth_mock_admin_subjects", "").split(",")))
    timestamp = datetime.now(UTC)
    users = {}
    for subject in sorted(subjects):
        if not subject.strip():
            raise RuntimeError("Blank legacy subject cannot be bound to a user")
        state, role = states.get(subject), roles.get(subject)
        owned = sessions_by_subject[subject]
        observed = observed_by_subject[subject]
        observed += [row["updated_at"] for row in (state, role) if row]
        user_time = state["sync_time"] or None if state else None
        status = state["status"] if state else ("active" if owned else "pending")
        # 旧角色处理器曾默认插入 active；只有角色且无登录/状态依据的身份改为 pending。
        if status == "active" and user_time is None and not owned:
            status = "pending"
        names = [row["display_name"] for row in owned if row["display_name"]]
        users[subject] = {
            "id": uuid4(),
            "iam_subject": subject,
            "display_name": names[-1] if names else None,
            "status": status,
            "role": ("admin" if role["authorized"] else "user")
            if role
            else ("admin" if subject in mock_admins else "user"),
            "user_sync_time": user_time,
            "role_sync_time": role["sync_time"] if role else None,
            "created_at": epoch(min(observed)) if observed else timestamp,
            "updated_at": epoch(max(observed)) if observed else timestamp,
        }
    converted_sessions = []
    for row in sessions:
        is_marker = row["scope"] == "slo"
        if row["scope"] not in {"slo", "platform", "user", "admin"}:
            raise RuntimeError("Unknown legacy authentication scope")
        if not re.fullmatch("[0-9a-f]{64}", row["ticket_hash"]):
            raise RuntimeError("Legacy ticket hash is invalid")
        if not is_marker and (
            not row["subject"] or not re.fullmatch("[0-9a-f]{64}", row["token_hash"])
        ):
            raise RuntimeError("Legacy session has invalid identity or token hash")
        if row["expires_at"] <= row["created_at"] or (is_marker and row["revoked_at"] is None):
            raise RuntimeError("Legacy session lifetime or revocation marker is invalid")
        revoked = epoch(row["revoked_at"])
        if not is_marker and (
            row["scope"] != "platform" or users[row["subject"]]["status"] != "active"
        ):
            revoked = revoked or timestamp
        converted_sessions.append(
            {
                "id": uuid4(),
                "user_id": None if is_marker else users[row["subject"]]["id"],
                "record_type": "ticket_revocation" if is_marker else "session",
                "token_hash": None if is_marker else row["token_hash"],
                "ticket_hash": row["ticket_hash"],
                "created_at": epoch(row["created_at"]),
                "expires_at": epoch(row["expires_at"]),
                "revoked_at": revoked,
            }
        )
    logs = []
    for row in events:
        if row["result"] != "success":
            continue
        if row["event"] not in {"login", "logout", "single_logout"}:
            raise RuntimeError("Unmapped successful authentication event")
        actor = (
            "iam" if row["event"] == "single_logout" else ("user" if row["subject"] else "system")
        )
        logs.append(
            {
                "id": identifier(row["id"]) or uuid4(),
                "actor_type": actor,
                "user_id": users[row["subject"]]["id"] if actor == "user" else None,
                "action": row["event"],
                "target_type": "session",
                "target_id": None,
                "request_id": identifier(row["request_id"]),
                "data": {"scope": row["scope"]},
                "created_at": epoch(row["created_at"]),
            }
        )
    return {
        "users": list(users.values()),
        "auth_sessions": converted_sessions,
        "operation_logs": logs,
    }


def create_user_trigger(connection):
    """数据库固定身份映射，仅资料、状态或角色发生实际变化时刷新更新时间。"""
    if connection.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE FUNCTION maintain_user_timestamps() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.iam_subject IS DISTINCT FROM OLD.iam_subject THEN
                RAISE EXCEPTION 'User identity mapping is immutable' USING ERRCODE = '23514';
            END IF;
            NEW.created_at := OLD.created_at;
            IF ROW(NEW.display_name, NEW.status, NEW.role)
               IS DISTINCT FROM ROW(OLD.display_name, OLD.status, OLD.role) THEN
                NEW.updated_at := CURRENT_TIMESTAMP;
            ELSE
                NEW.updated_at := OLD.updated_at;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_users_timestamps BEFORE UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION maintain_user_timestamps()
    """)


def upgrade() -> None:
    """在线迁移并删除旧六表，登录上下文切换至 Redis；业务归属契约保持不变。"""
    if context.is_offline_mode():
        raise RuntimeError("Authentication consolidation requires an online database connection")
    connection = op.get_bind()
    options = context.get_x_argument(as_dictionary=True)
    rows = read_legacy(connection)
    converted = transform(rows, options)
    archive_legacy(rows, options)
    create_tables()
    metadata = sa.MetaData()
    for name, values in converted.items():
        columns = [sa.Column("id", sa.Uuid(), primary_key=True)]
        if name in {"auth_sessions", "operation_logs"}:
            columns.append(sa.Column("user_id", sa.Uuid()))
        if name == "operation_logs":
            columns.append(sa.Column("request_id", sa.Uuid()))
        table = sa.Table(name, metadata, *columns, autoload_with=connection)
        if values:
            connection.execute(table.insert(), values)
        if connection.scalar(sa.select(sa.func.count()).select_from(table)) != len(values):
            raise RuntimeError("Authentication conversion count mismatch")
    create_user_trigger(connection)
    # 旧回执摘要包括 eventId 且不含可还原的报文，不能冒充新的规范化摘要。
    # 保存最新用户版本后，重试相同内容仍不会重复执行状态变更或记录成功日志。
    for name in LEGACY_TABLES:
        op.drop_table(name)


def downgrade() -> None:
    """合并后无法无损恢复旧回执和失败日志，禁止静默删表式回退。"""
    raise RuntimeError(
        "Authentication consolidation requires restoring the pre-upgrade database backup "
        "with its matching application version; do not stamp backwards"
    )
