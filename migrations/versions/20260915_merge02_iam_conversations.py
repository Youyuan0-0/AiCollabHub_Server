"""汇合身份同步与会话分表迁移，保留双方已使用的版本号。"""

revision = "20260915_merge02"
down_revision = ("20260915_02", "20260915_merge01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只汇合版本；缺失的身份或会话迁移由 Alembic 先执行，不重复修改已有表。"""
    pass


def downgrade() -> None:
    """恢复两个父版本标记，保留身份、会话及其他业务数据。"""
    pass
