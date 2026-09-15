"""首页会话独立版本链；支持共享库及本模块空库，不假定其他模块的迁移编号。"""

from importlib import import_module

revision = "20260915_chat01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """复用已验证的数据迁移；全量迁移已建表时先严格检查结构再登记模块版本。"""
    implementation = import_module("migrations.versions.20260915_chat01_conversation_messages")
    implementation.upgrade()


def downgrade():
    """回退本模块数据，不更改共享库的全局迁移版本或其他业务表。"""
    implementation = import_module("migrations.versions.20260915_chat01_conversation_messages")
    implementation.downgrade()
