"""系统日志统一为信息、警告和错误三个级别。"""

import sqlalchemy as sa
from alembic import op

revision = "20260918_02"
down_revision = "20260918_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留历史日志，将调试归入信息、严重错误归入错误后收紧约束。"""
    op.execute("UPDATE system_logs SET level = 'info' WHERE level = 'debug'")
    op.execute("UPDATE system_logs SET level = 'error' WHERE level = 'critical'")
    with op.batch_alter_table("system_logs") as batch:
        batch.drop_constraint(op.f("ck_system_logs_level"), type_="check")
        batch.create_check_constraint(
            op.f("ck_system_logs_level"), "level IN ('info', 'warning', 'error')"
        )
        batch.alter_column(
            "level", existing_type=sa.String(16), comment="日志级别：info/warning/error"
        )


def downgrade() -> None:
    """恢复可选级别；已归类记录无法推断原级别，继续保留其当前值。"""
    with op.batch_alter_table("system_logs") as batch:
        batch.drop_constraint(op.f("ck_system_logs_level"), type_="check")
        batch.create_check_constraint(
            op.f("ck_system_logs_level"),
            "level IN ('debug', 'info', 'warning', 'error', 'critical')",
        )
        batch.alter_column(
            "level",
            existing_type=sa.String(16),
            comment="日志级别：debug/info/warning/error/critical",
        )
