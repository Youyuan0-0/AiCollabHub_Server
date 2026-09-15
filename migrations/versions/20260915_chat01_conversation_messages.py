"""拆分旧 JSON 会话，迁移完整消息、用户归属、附件元信息与删除标记。"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import context, op
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.dialects.postgresql import JSONB

revision = "20260915_chat01"
down_revision = "20260914_03"
branch_labels = None
depends_on = None


def timestamp(value):
    """旧无时区时间按 UTC 解释，有时区字符串统一转换为 UTC。"""
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime):
        raise ValueError("invalid timestamp")
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def legacy_record(row):
    """冻结旧 JSON 转换规则，避免未来接口模型变化影响历史迁移；坏记录中止而不跳过。"""
    created = timestamp(row["created_at"])
    base = {
        "id": row["id"],
        "title": "",
        "model": "",
        "knowledge_base": "",
        "tools": [],
        "owner_id": "",
        "created_at": created,
        "updated_at": created,
        "version": 0,
        "deleted": row["deleted"],
    }
    if row["deleted"]:
        return base, []
    try:
        data = json.loads(row["payload"])
        if data["id"] != row["id"]:
            raise ValueError("mismatched identifier")
        if "messages" not in data:
            # 最早的单轮 ChatAnswer 升级为两条消息；保持既有派生消息编号。
            data = {
                "id": row["id"],
                "title": data["question"][:200],
                "model": data["model"],
                "knowledgeBase": data["knowledgeBase"],
                "createdAt": created,
                "updatedAt": created,
                "messages": [
                    {
                        "id": f"{row['id']}-{role}",
                        "role": role,
                        "content": text,
                        "createdAt": created,
                        "model": data["model"],
                        "knowledgeBase": data["knowledgeBase"],
                        "sources": sources,
                    }
                    for role, text, sources in (
                        ("user", data["question"], []),
                        ("assistant", data["text"], data["sources"]),
                    )
                ],
            }
        base.update(
            title=data["title"],
            model=data["model"],
            knowledge_base=data["knowledgeBase"],
            tools=data.get("tools", []),
            owner_id=data.get("ownerId", ""),
            created_at=timestamp(data["createdAt"]),
            updated_at=timestamp(data["updatedAt"]),
            version=data.get("version", 0),
        )
        if not isinstance(base["version"], int) or base["version"] < 0:
            raise ValueError("invalid version")
        if not all(
            isinstance(base[key], str) for key in ("title", "model", "knowledge_base", "owner_id")
        ):
            raise ValueError("invalid summary")
        if not isinstance(base["tools"], list) or not isinstance(data["messages"], list):
            raise ValueError("invalid list")
        messages = []
        for position, item in enumerate(data["messages"]):
            message = {
                "conversation_id": row["id"],
                "position": position,
                "id": item["id"],
                "role": item["role"],
                "content": item["content"],
                "created_at": timestamp(item["createdAt"]),
                "model": item["model"],
                "knowledge_base": item["knowledgeBase"],
                "tools": item.get("tools", []),
                "sources": item.get("sources", []),
                "attachments": item.get("attachments", []),
            }
            if message["role"] not in {"user", "assistant"}:
                raise ValueError("invalid role")
            if not all(
                isinstance(message[key], str)
                for key in ("id", "content", "model", "knowledge_base")
            ):
                raise ValueError("invalid message")
            if not all(
                isinstance(message[key], list) for key in ("tools", "sources", "attachments")
            ):
                raise ValueError("invalid metadata")
            messages.append(message)
        return base, messages
    except (KeyError, TypeError, ValueError):
        # 错误不携带问题内容、附件信息或原始 payload；修复旧数据后再执行迁移。
        raise RuntimeError(
            "Invalid legacy conversation; migration aborted without skipping data"
        ) from None


def conversation_metadata():
    """冻结此迁移版本的表定义，后续业务模型增删字段不会改变旧迁移的校验规则。"""
    metadata = sa.MetaData()
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    conversations = sa.Table(
        "conversations",
        metadata,
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("knowledge_base", sa.Text(), nullable=False),
        sa.Column("tools", json_type, nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.CheckConstraint("version >= 0", name=op.f("ck_conversations_version_nonnegative")),
    )
    sa.Index(
        "ix_conversations_history",
        conversations.c.owner_id,
        conversations.c.deleted,
        conversations.c.updated_at,
        conversations.c.id,
    )
    sa.Table(
        "conversation_messages",
        metadata,
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("knowledge_base", sa.Text(), nullable=False),
        sa.Column("tools", json_type, nullable=False),
        sa.Column("sources", json_type, nullable=False),
        sa.Column("attachments", json_type, nullable=False),
        sa.PrimaryKeyConstraint(
            "conversation_id", "position", name=op.f("pk_conversation_messages")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
            name=op.f("fk_conversation_messages_conversation_id_conversations"),
        ),
        sa.CheckConstraint(
            "position >= 0", name=op.f("ck_conversation_messages_position_nonnegative")
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')", name=op.f("ck_conversation_messages_role_valid")
        ),
    )
    return metadata


def current_schema_exists(bind):
    """独立入口可能已经完成分表；校验一致后接管，避免全量升级重复建表。"""
    existing = set(sa.inspect(bind).get_table_names())
    expected = {"conversations", "conversation_messages"}
    if not existing & expected:
        return False
    if not expected <= existing:
        raise RuntimeError("Incomplete conversation tables; migration aborted")
    metadata = conversation_metadata()
    migration_context = MigrationContext.configure(
        bind,
        opts={
            "compare_type": True,
            "include_object": lambda obj, name, kind, reflected, compared: (
                kind != "table" or name in expected
            ),
        },
    )
    if compare_metadata(migration_context, metadata):
        raise RuntimeError("Existing conversation schema differs from the expected schema")
    if "demo_conversations" in existing:
        if bind.dialect.name == "postgresql":
            bind.execute(sa.text("LOCK TABLE demo_conversations IN ACCESS EXCLUSIVE MODE"))
        legacy = sa.Table("demo_conversations", sa.MetaData(), autoload_with=bind)
        if bind.execute(sa.select(legacy.c.id).limit(1)).first() is not None:
            # 两套表都有来源时不猜测覆盖关系；中止并保留全部记录供核对。
            raise RuntimeError("Ambiguous conversation tables; migration aborted")
        # 兼容补表迁移在分表后重建的空表，不保留第二套存储。
        op.drop_table("demo_conversations")
    return True


def upgrade():
    """在线迁移先校验所有旧数据，再建新表并搬迁；仅成功后删除旧表，保留原 ID。"""
    if context.is_offline_mode():
        raise RuntimeError("Conversation data migration requires an online database connection")
    bind = op.get_bind()
    if current_schema_exists(bind):
        return
    # 某些旧分支仅推进了版本号，缺少旧表；空表也走相同的转换流程。
    op.create_table(
        "demo_conversations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_conversations")),
        if_not_exists=True,
    )
    if bind.dialect.name == "postgresql":
        # 搬迁期间阻止旧 API 写入，确保读取快照与最终删除旧表之间没有遗漏的消息。
        bind.execute(sa.text("LOCK TABLE demo_conversations IN ACCESS EXCLUSIVE MODE"))
    legacy = sa.Table("demo_conversations", sa.MetaData(), autoload_with=bind)
    records = [legacy_record(row) for row in bind.execute(sa.select(legacy)).mappings()]
    metadata = conversation_metadata()
    metadata.create_all(bind, checkfirst=False)
    conversations = metadata.tables["conversations"]
    messages = metadata.tables["conversation_messages"]
    for record, items in records:
        bind.execute(conversations.insert().values(**record))
        if items:
            bind.execute(messages.insert(), items)
    op.drop_table("demo_conversations")


def downgrade():
    """回退前将当前会话重新打包为旧 JSON，包含升级后的新消息，避免仅恢复过期快照。"""
    if context.is_offline_mode():
        raise RuntimeError("Conversation data migration requires an online database connection")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 回退同样先锁会话再锁消息，与在线写入顺序一致，保证回填旧 JSON 的一致性。
        bind.execute(
            sa.text("LOCK TABLE conversations, conversation_messages IN ACCESS EXCLUSIVE MODE")
        )
    metadata = sa.MetaData()
    conversations = sa.Table("conversations", metadata, autoload_with=bind)
    messages = sa.Table("conversation_messages", metadata, autoload_with=bind)
    legacy = op.create_table(
        "demo_conversations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_conversations")),
    )
    for row in bind.execute(sa.select(conversations)).mappings():
        payload = {}
        if not row["deleted"]:
            payload = {
                "id": row["id"],
                "title": row["title"],
                "model": row["model"],
                "knowledgeBase": row["knowledge_base"],
                "tools": row["tools"],
                "ownerId": row["owner_id"],
                "createdAt": timestamp(row["created_at"]).isoformat(),
                "updatedAt": timestamp(row["updated_at"]).isoformat(),
                "version": row["version"],
                "messages": [
                    {
                        "id": item["id"],
                        "role": item["role"],
                        "content": item["content"],
                        "createdAt": timestamp(item["created_at"]).isoformat(),
                        "model": item["model"],
                        "knowledgeBase": item["knowledge_base"],
                        "tools": item["tools"],
                        "sources": item["sources"],
                        "attachments": item["attachments"],
                    }
                    for item in bind.execute(
                        sa.select(messages)
                        .where(messages.c.conversation_id == row["id"])
                        .order_by(messages.c.position)
                    ).mappings()
                ],
            }
        bind.execute(
            legacy.insert().values(
                id=row["id"],
                payload=json.dumps(payload, ensure_ascii=False),
                created_at=timestamp(row["created_at"]).replace(tzinfo=None),
                deleted=row["deleted"],
            )
        )
    op.drop_table("conversation_messages")
    op.drop_index("ix_conversations_history", table_name="conversations")
    op.drop_table("conversations")
