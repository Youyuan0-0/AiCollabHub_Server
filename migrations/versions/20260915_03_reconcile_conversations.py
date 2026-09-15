"""补齐历史版本标记已前进但遗漏的开发会话表，不改动已有数据。"""

import sqlalchemy as sa
from alembic import op

revision = "20260915_03"
down_revision = "20260915_02"
branch_labels = None
depends_on = None


def upgrade():
    # 历史分支合并后部分库跳过了原建表版本；已存在时保持原表及记录。
    op.create_table(
        "demo_conversations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_conversations")),
        if_not_exists=True,
    )


def downgrade():
    # 该表属于 20260914_02；只回退补齐版本不能删除恢复后的会话数据。
    pass
