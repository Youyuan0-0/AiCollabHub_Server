"""汇合知识库与系统日志迁移，保留两侧已部署版本的升级路径。"""

revision = "20260921_merge_knowledge_logs"
down_revision = ("20260920_01_knowledge", "20260918_02")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两个父分支已分别建表，汇合节点不重复修改业务数据。"""
    pass


def downgrade() -> None:
    """仅拆分版本汇合点，表结构由各父分支负责回滚。"""
    pass
