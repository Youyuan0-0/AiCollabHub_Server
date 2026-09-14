from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import Settings
from app.db.base import Base
from app.db.conversations import DemoConversation  # noqa: F401
from app.db.overview import OverviewSnapshot  # noqa: F401

# 复用应用的数据库配置，不在 alembic.ini 中另外保存连接凭据。
settings = Settings()
database_setting = settings.database_migration_url or settings.database_url
if database_setting is None:
    raise RuntimeError("DATABASE_URL or DATABASE_MIGRATION_URL is required for migrations")

database_url = database_setting.get_secret_value()
# 导入会话和看板模型以注册表定义；保留迁移专用账号与旧表删除保护。
target_metadata = Base.metadata


def include_object(obj, name, object_type, reflected, compare_to):
    # 框架阶段不接管旧测试表；未在当前模型声明的表不会被自动生成删除操作。
    return not (object_type == "table" and reflected and compare_to is None)


def run_migrations_offline() -> None:
    # 离线模式只生成 SQL 脚本，不建立数据库连接，便于部署前审阅。
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # 迁移是短时命令，不保留连接池；命令结束后释放数据库连接资源。
    engine = create_engine(database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
