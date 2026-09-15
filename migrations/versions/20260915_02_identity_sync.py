"""最小身份状态、角色授权和同步回执，不保存集团完整用户档案。"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_02"
down_revision = "20260915_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "iam_identity_state",
        sa.Column("subject", sa.String(200), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled', 'deleted')", name="valid_status"),
        sa.CheckConstraint("sync_time >= 0", name="valid_sync_time"),
    )
    op.create_table(
        "iam_identity_role",
        sa.Column(
            "subject", sa.String(200), sa.ForeignKey("iam_identity_state.subject"), primary_key=True
        ),
        sa.Column("role_code", sa.String(64), primary_key=True),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("sync_time > 0", name="valid_sync_time"),
    )
    op.create_table(
        "iam_sync_receipt",
        sa.Column("event_id", sa.String(100), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column("applied", sa.Integer(), nullable=False),
        sa.Column("ignored", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("revoked_sessions", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.Column("last_received_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_iam_sync_receipt_received_at", "iam_sync_receipt", ["received_at"])
    op.create_index("ix_auth_session_subject", "auth_session", ["subject"], if_not_exists=True)


def downgrade():
    op.drop_index("ix_auth_session_subject", table_name="auth_session")
    op.drop_table("iam_sync_receipt")
    op.drop_table("iam_identity_role")
    op.drop_table("iam_identity_state")
