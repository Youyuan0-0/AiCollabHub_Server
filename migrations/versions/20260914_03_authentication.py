"""合并首页与登录迁移，兼容两条旧分支曾共用的 20260914_01 版本。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_03"
down_revision = "20260914_02"
branch_labels = None
depends_on = None


def upgrade():
    # 两条旧分支共用版本号，按实际表结构补齐；保留已有会话、看板和认证记录。
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "overview_snapshots" not in existing:
        op.create_table(
            "overview_snapshots",
            sa.Column("section", sa.String(32), nullable=False),
            sa.Column("variant", sa.String(128), nullable=False),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("section", "variant", name=op.f("pk_overview_snapshots")),
        )

    if "auth_session" not in existing:
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
        )
        op.create_index("ix_auth_session_expires_at", "auth_session", ["expires_at"])
    if "auth_login_request" not in existing:
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
        )
        op.create_index("ix_auth_login_request_expires_at", "auth_login_request", ["expires_at"])
    if "auth_event_log" not in existing:
        op.create_table(
            "auth_event_log",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("subject", sa.String(200)),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("event", sa.String(40), nullable=False),
            sa.Column("result", sa.String(40), nullable=False),
            sa.Column("request_id", sa.String(36), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
        )
        op.create_index("ix_auth_event_log_created_at", "auth_event_log", ["created_at"])
        op.create_index(
            "ix_auth_event_log_subject_created", "auth_event_log", ["subject", "created_at"]
        )


def downgrade():
    op.drop_table("auth_event_log")
    op.drop_table("auth_login_request")
    op.drop_table("auth_session")
