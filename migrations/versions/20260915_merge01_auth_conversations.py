"""汇合认证兼容标记与会话分表版本，保留双方已部署的迁移历史。"""

revision = "20260915_merge01"
down_revision = ("20260915_01", "20260915_chat01")
branch_labels = None
depends_on = None


def upgrade():
    """仅汇合版本；Alembic 先执行缺失的父分支，已有业务表不在此重复修改。"""
    pass


def downgrade():
    """仅拆回两个父版本；数据回退由会话分支负责，认证兼容标记不修改表。"""
    pass
