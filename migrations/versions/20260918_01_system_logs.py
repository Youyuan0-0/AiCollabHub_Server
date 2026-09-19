"""新增系统运行事件与异常日志表。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260918_01"
down_revision = "20260917_merge_ds_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建系统日志表及排查所需索引，不修改已有业务表。"""
    op.create_table(
        "system_logs",
        sa.Column("id", sa.Uuid(), nullable=False, comment="系统日志唯一编号"),
        sa.Column(
            "level",
            sa.String(16),
            nullable=False,
            comment="日志级别：debug/info/warning/error/critical",
        ),
        sa.Column("source", sa.String(64), nullable=False, comment="产生日志的模块或组件"),
        sa.Column("event", sa.String(100), nullable=False, comment="稳定的技术事件编码"),
        sa.Column("message", sa.Text(), nullable=False, comment="便于阅读的事件或异常摘要"),
        sa.Column(
            "request_id",
            sa.Uuid(),
            nullable=True,
            comment="关联后端请求 UUID；后台事件可为空，同一请求可重复",
        ),
        sa.Column(
            "details",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment="已脱敏的结构化上下文，仅保存 JSON 对象",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="事件发生时间，统一使用 UTC",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_logs")),
        sa.CheckConstraint(
            "level IN ('debug', 'info', 'warning', 'error', 'critical')",
            name=op.f("ck_system_logs_level"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name=op.f("ck_system_logs_details")
        ).ddl_if(dialect="postgresql"),
        comment="系统运行事件与异常日志，面向技术运维排查",
    )
    op.create_index("ix_system_logs_created_at", "system_logs", ["created_at"], unique=False)
    op.create_index(
        "ix_system_logs_request_id",
        "system_logs",
        ["request_id"],
        unique=False,
        postgresql_where=sa.text("request_id IS NOT NULL"),
        sqlite_where=sa.text("request_id IS NOT NULL"),
    )


def downgrade() -> None:
    """回退时仅删除本迁移创建的系统日志表及索引。"""
    op.drop_index("ix_system_logs_request_id", table_name="system_logs")
    op.drop_index("ix_system_logs_created_at", table_name="system_logs")
    op.drop_table("system_logs")
