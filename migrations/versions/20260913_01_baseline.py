"""Framework baseline; no business tables are part of the current template."""

revision = "20260913_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # 保留已有测试库的版本锚点，不创建、修改或删除远端历史测试表。
    pass


def downgrade():
    pass
