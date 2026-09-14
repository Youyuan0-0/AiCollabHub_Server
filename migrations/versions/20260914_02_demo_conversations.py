"""增加开发问答历史表，不插入固定样例，不影响正式会话设计。"""

import sqlalchemy as sa
from alembic import op

revision = "20260914_02"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存新问答及样例删除标记，保证刷新或重启后的历史一致。"""
    op.create_table(
        "demo_conversations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_conversations")),
    )


def downgrade() -> None:
    """回退删除开发历史及删除标记，固定样例文件仍保留。"""
    op.drop_table("demo_conversations")
