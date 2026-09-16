"""汇合场景分支与单库分支的迁移历史，并补齐旧身份迁移遗漏的会话索引。"""

from alembic import op

revision = "20260916_01"
down_revision = ("20260915_04", "20260915_merge02")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 两个分支曾各自定义 20260915_02；已执行过该编号的库不会重跑其建表语句。
    op.create_index("ix_auth_session_subject", "auth_session", ["subject"], if_not_exists=True)


def downgrade() -> None:
    # 原身份分支已包含该索引，回退汇合版本仍保留它及全部业务数据。
    pass
