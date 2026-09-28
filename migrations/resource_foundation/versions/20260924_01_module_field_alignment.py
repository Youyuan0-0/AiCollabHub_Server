"""补齐数据集交付元数据字段。"""

import sqlalchemy as sa
from alembic import op

revision = "20260924_01_field_alignment"
down_revision = "20260922_02_tool_alignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增血缘、存储位置和 API 地址，历史记录保持未填写状态。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    op.add_column(
        "datasets",
        sa.Column("data_lineage", sa.Text(), nullable=True, comment="数据来源、加工及流转血缘"),
    )
    op.add_column(
        "datasets",
        sa.Column("storage_location", sa.String(1000), nullable=True, comment="数据存储位置"),
    )
    op.add_column(
        "datasets",
        sa.Column("api_endpoint", sa.String(2000), nullable=True, comment="数据访问 API 地址"),
    )


def downgrade() -> None:
    """移除本次新增且不被其他表引用的交付元数据。"""
    op.drop_column("datasets", "api_endpoint")
    op.drop_column("datasets", "storage_location")
    op.drop_column("datasets", "data_lineage")
