"""数据集独立迁移，保留共享数据库全局版本及其他模块数据。"""

from importlib import import_module

revision = "20260916_ds01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """复用全量迁移；已存在的数据集表先校验结构，避免重复建表或丢失数据。"""
    implementation = import_module("migrations.versions.20260916_02_dataset_workspace")
    implementation.upgrade()


def downgrade() -> None:
    """共享表可能已被全量迁移接管，独立入口不允许删除业务数据。"""
    raise RuntimeError("Dataset rollback requires an explicit backup and deployment review")
