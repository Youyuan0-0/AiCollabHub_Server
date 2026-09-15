"""首页会话独立版本链；支持共享库及本模块空库，不假定其他模块的迁移编号。"""

from importlib import import_module

import sqlalchemy as sa
from alembic import op
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

revision = "20260915_chat01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    """复用已验证的数据迁移；全量迁移已建表时先严格检查结构再登记模块版本。"""
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    expected = {"conversations", "conversation_messages"}
    if existing & expected:
        if not expected <= existing or "demo_conversations" in existing:
            raise RuntimeError("Ambiguous conversation tables; migration aborted")
        # 只有完全一致的既有结构才允许接管，不能将不完整的表简单标记为已迁移。
        from app.db.conversations import Conversation, StoredConversationMessage

        metadata = sa.MetaData()
        for table in (Conversation.__table__, StoredConversationMessage.__table__):
            table.to_metadata(metadata)
        context = MigrationContext.configure(
            bind,
            opts={
                "compare_type": True,
                "include_object": lambda obj, name, kind, reflected, compared: (
                    kind != "table" or name in expected
                ),
            },
        )
        if compare_metadata(context, metadata):
            raise RuntimeError("Existing conversation schema differs from the expected schema")
        return
    if "demo_conversations" not in existing:
        # 模块空库不需要伪造全局版本，仅用空旧表复用同一套建表/迁移规则。
        op.create_table(
            "demo_conversations",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("deleted", sa.Boolean(), nullable=False),
        )
    implementation = import_module("migrations.versions.20260915_chat01_conversation_messages")
    implementation.upgrade()


def downgrade():
    """回退本模块数据，不更改共享库的全局迁移版本或其他业务表。"""
    implementation = import_module("migrations.versions.20260915_chat01_conversation_messages")
    implementation.downgrade()
