"""合并首页与登录迁移，兼容两条旧分支曾共用的 20260914_01 版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_03"
down_revision = "20260914_02"
branch_labels = None
depends_on = None


def upgrade():
    # 旧认证库的 20260914_01 不包含看板，补齐缺表并保留已有数据。
    # 使用条件 DDL 兼容在线升级和离线 PostgreSQL SQL，不需要先连接数据库反射表。
    op.create_table(
        "overview_snapshots",
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("variant", sa.String(128), nullable=False),
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
    # 看板由前序 20260914_01 管理，本次只回退认证三表。
    op.drop_table("auth_event_log")
    op.drop_table("auth_login_request")
    op.drop_table("auth_session")
