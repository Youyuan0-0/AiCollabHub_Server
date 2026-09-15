"""恢复远端已使用的身份同步迁移；结构按 2026-09-15 实际数据库核对。"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_02"
down_revision = "20260915_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 已处于本版本的远端库由 Alembic 跳过 DDL；较早版本正常创建身份同步表。
    # 不以 stamp 或修改历史版本号代替迁移，也不接管独立创建的 scene_* 表。
    op.create_table(
        "iam_identity_state",
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("subject", name=op.f("pk_iam_identity_state")),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deleted')",
            name=op.f("ck_iam_identity_state_valid_status"),
        ),
        sa.CheckConstraint("sync_time >= 0", name=op.f("ck_iam_identity_state_valid_sync_time")),
    )
    op.create_table(
        "iam_identity_role",
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("role_code", sa.String(64), nullable=False),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("sync_time", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("subject", "role_code", name=op.f("pk_iam_identity_role")),
        sa.ForeignKeyConstraint(
            ["subject"],
            ["iam_identity_state.subject"],
            name=op.f("fk_iam_identity_role_subject_iam_identity_state"),
        ),
        sa.CheckConstraint("sync_time > 0", name=op.f("ck_iam_identity_role_valid_sync_time")),
    )
    op.create_table(
        "iam_sync_receipt",
        sa.Column("event_id", sa.String(100), nullable=False),
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
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_iam_sync_receipt")),
    )
    op.create_index("ix_iam_sync_receipt_received_at", "iam_sync_receipt", ["received_at"])


def downgrade() -> None:
    op.drop_table("iam_sync_receipt")
    op.drop_table("iam_identity_role")
    op.drop_table("iam_identity_state")
