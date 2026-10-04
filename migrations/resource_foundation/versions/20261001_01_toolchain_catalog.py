"""独立保存华为外部目录快照和轮询状态，不改变业务主表及本地文件约束。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261001_01_toolchain_catalog"
down_revision = "20260924_01_field_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存脱敏目录及跨 worker 租约，不存供应方配置或 API Key。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    op.create_table(
        "toolchain_catalog_snapshots",
        sa.Column(
            "connection_key", sa.String(64), primary_key=True, comment="模块、环境和凭据范围的摘要"
        ),
        sa.Column("module_code", sa.String(32), nullable=False, comment="工具链业务模块"),
        sa.Column("provider", sa.String(32), nullable=False, comment="外部供应方"),
        sa.Column("data", JSONB(), nullable=False, comment="脱敏后的外部目录；不是平台正式资源"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), comment="最近开始查询时间"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), comment="最近完整查询成功时间"),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), comment="下次允许自动查询时间"),
        sa.Column("last_error", sa.String(500), nullable=False, comment="脱敏后的最近查询错误"),
        sa.Column("failure_count", sa.Integer(), nullable=False, comment="连续查询失败次数"),
        sa.Column("lease_token", sa.String(32), comment="当前查询持有者令牌"),
        sa.Column("lease_until", sa.DateTime(timezone=True), comment="当前查询租约过期时间"),
        sa.CheckConstraint(
            "module_code IN ('applications','datasets','knowledge','models')", name="module_code"
        ),
        sa.CheckConstraint("provider='huawei'", name="provider"),
        sa.CheckConstraint("failure_count>=0", name="failure_count"),
        sa.CheckConstraint("jsonb_typeof(data)='object'", name="data"),
        sa.CheckConstraint("(lease_token IS NULL)=(lease_until IS NULL)", name="lease"),
        comment="外部工具链目录快照、最近查询结果和定期轮询租约",
    )


def downgrade() -> None:
    """目录缓存可通过重新查询恢复，删除前先停用华为轮询。"""
    op.drop_table("toolchain_catalog_snapshots")
