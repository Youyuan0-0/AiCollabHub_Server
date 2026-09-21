"""新增独立知识库记录表，不修改数据集和场景应用数据。"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_01_knowledge"
down_revision = "20260917_merge_ds_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建带乐观锁的知识库快照表，文档原文件由持久卷保存。"""
    op.create_table(
        "knowledge_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """回滚仅删除本模块快照表，原文件由运维确认后单独清理。"""
    op.drop_table("knowledge_records")
