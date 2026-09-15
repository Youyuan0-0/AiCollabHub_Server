"""汇合身份同步与会话分表，保留已部署版本并清理补表产生的空旧表。"""

from importlib import import_module

from alembic import context, op

revision = "20260915_04"
down_revision = ("20260915_merge01", "20260915_03")
branch_labels = None
depends_on = None


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("Conversation data migration requires an online database connection")
    implementation = import_module("migrations.versions.20260915_chat01_conversation_messages")
    if not implementation.current_schema_exists(op.get_bind()):
        raise RuntimeError("Conversation tables are missing after merging migrations")


def downgrade():
    # 仅拆分版本；现有消息和身份数据由各自父迁移处理，不在此删除。
    pass
