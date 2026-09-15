"""接入认证三表，并兼容两个分支曾共用 20260914_01 的历史测试库。"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_01"
down_revision = "20260914_02"
branch_labels = None
depends_on = None


def upgrade():
    # 旧认证库记录的 20260914_01 不包含看板；补齐缺表，保留已有快照和登录数据。
    # IF NOT EXISTS 同时支持在线升级与离线 SQL，无需重新 stamp 或重建已有表。
    op.create_table(
        "overview_snapshots",
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("variant", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("section", "variant", name=op.f("pk_overview_snapshots")),
        if_not_exists=True,
    )
    op.create_table(
        "auth_session",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("ticket_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(200)),
        sa.Column("display_name", sa.String(100)),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger()),
        if_not_exists=True,
    )
    op.create_index(
        "ix_auth_session_expires_at", "auth_session", ["expires_at"], if_not_exists=True
    )
    op.create_table(
        "auth_login_request",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("service_url", sa.Text(), nullable=False),
        sa.Column("return_path", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.BigInteger()),
        if_not_exists=True,
    )
    op.create_index(
        "ix_auth_login_request_expires_at", "auth_login_request", ["expires_at"], if_not_exists=True
    )
    op.create_table(
        "auth_event_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(200)),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("result", sa.String(40), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        "ix_auth_event_log_created_at", "auth_event_log", ["created_at"], if_not_exists=True
    )
    op.create_index(
        "ix_auth_event_log_subject_created",
        "auth_event_log",
        ["subject", "created_at"],
        if_not_exists=True,
    )


def downgrade():
    # 看板属于 20260914_01，回退本次认证迁移时保留，交给原迁移负责删除。
    op.drop_table("auth_event_log")
    op.drop_table("auth_login_request")
    op.drop_table("auth_session")
