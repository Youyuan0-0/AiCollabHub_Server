"""创建首页及运营快照表，不自动导入演示数据。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立板块与筛选键的联合主键，重复发布同一范围时只更新已有快照。"""
    op.create_table(
        "overview_snapshots",
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("variant", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("section", "variant", name=op.f("pk_overview_snapshots")),
    )


def downgrade() -> None:
    """回退会删除全部看板快照，部署回退前应自行备份已发布内容。"""
    op.drop_table("overview_snapshots")
