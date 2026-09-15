"""共享数据库的首页会话迁移入口，不修改全局 alembic_version。"""

from alembic import context
from sqlalchemy import MetaData, create_engine, pool, text

from app.core.config import Settings
from app.db.conversations import Conversation, StoredConversationMessage
from app.db.session import normalize_database_url

# 命令行默认读取 .env；验证脚本可显式传入同一配置对象，避免误读运行目录的配置。
settings = context.config.attributes.get("settings") or Settings()
database_setting = settings.database_migration_url or settings.database_url
if database_setting is None:
    raise RuntimeError("Database configuration is required for conversation migrations")
url = normalize_database_url(database_setting.get_secret_value())
# 只比较本模块表，防止自动迁移接管认证、场景应用等其他模块的表。
metadata = MetaData()
for table in (Conversation.__table__, StoredConversationMessage.__table__):
    table.to_metadata(metadata)


def include_object(obj, name, object_type, reflected, compare_to):
    """独立迁移只管理会话表及消息表，其他已存在的表不进入差异检查。"""
    return object_type != "table" or name in metadata.tables


def run_migrations():
    """使用主库和独立版本表执行事务；锁等待有上限，失败不改写其他模块版本。"""
    if context.is_offline_mode():
        raise RuntimeError("Conversation data migration requires an online database connection")
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            # 显式事务使 PostgreSQL 的表变更、旧数据搬迁和模块版本写入一起提交。
            with connection.begin():
                if connection.dialect.name == "postgresql":
                    if connection.scalar(text("SELECT pg_is_in_recovery()")):
                        raise RuntimeError("Conversation migrations require the primary database")
                    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                context.configure(
                    connection=connection,
                    target_metadata=metadata,
                    version_table="alembic_version_conversations",
                    compare_type=True,
                    include_object=include_object,
                )
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
