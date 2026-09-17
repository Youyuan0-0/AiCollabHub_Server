"""数据集用户端发布和活动记录，后续审核流程可沿用申请修订号。"""

import sqlalchemy as sa
from alembic import context, op

revision = "20260916_02"
down_revision = "20260916_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立数据集业务表；独立迁移已建表时校验结构并保留全部记录。"""
    # 冻结本版本结构，不依赖未来业务模型；不把同名但不兼容的旧表当作迁移成功。
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if inspector.has_table("dataset_records"):
            columns = {
                column["name"]: column for column in inspector.get_columns("dataset_records")
            }
            valid = set(columns) == {"key", "revision", "data"}
            if valid:
                valid = (
                    isinstance(columns["key"]["type"], sa.String)
                    and columns["key"]["type"].length == 240
                    and isinstance(columns["revision"]["type"], sa.Integer)
                    and isinstance(columns["data"]["type"], sa.JSON)
                    and all(not column["nullable"] for column in columns.values())
                    and inspector.get_pk_constraint("dataset_records")["constrained_columns"]
                    == ["key"]
                )
            if not valid:
                raise RuntimeError("Existing dataset_records schema does not match the migration")
            return
    op.create_table(
        "dataset_records",
        sa.Column("key", sa.String(240), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    """回退数据集业务表；仅由部署人员显式执行。"""
    op.drop_table("dataset_records")
