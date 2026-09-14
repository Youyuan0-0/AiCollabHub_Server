from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import Settings
from app.db.base import Base
# 注册开发会话表，使后续自动迁移能够识别历史存储。
from app.db.conversations import DemoConversation  # noqa: F401
from app.db.overview import OverviewSnapshot  # noqa: F401

# 复用应用的数据库配置，不在 alembic.ini 中另外保存连接凭据。
settings = Settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL must be configured before running migrations")

database_url = settings.database_url.get_secret_value()
# 导入看板模型以注册表定义，保证自动迁移能识别 overview_snapshots。
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    # 离线模式只生成 SQL 脚本，不建立数据库连接，便于部署前审阅。
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
