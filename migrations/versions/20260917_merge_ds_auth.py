"""汇合数据集与反馈、认证分支，保留双方已部署版本和业务数据。"""

revision = "20260917_merge_ds_auth"
down_revision = ("20260916_02", "20260917_auth01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """两条分支分别完成建表与兼容检查，汇合只登记版本，不改动业务数据。"""
    pass


def downgrade() -> None:
    """仅退回两个父版本，不在汇合节点删除任一模块的表。"""
    pass
