"""为重新设计后的空库执行版本化迁移，不引入旧业务表。"""

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.core.config import Settings


def configure(connection) -> None:
    """允许测试传入隔离事务；版本记录与业务表使用同一目标 schema。"""
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(2026092001)"))
        context.run_migrations()


def run() -> None:
    """复用项目配置读取凭据，命令退出后释放所有迁移连接。"""
    supplied = context.config.attributes.get("connection")
    if supplied is not None:
        configure(supplied)
        return
    settings = Settings()
    secret = settings.database_migration_url or settings.database_url
    if secret is None:
        raise RuntimeError("Database configuration is required")
    if context.is_offline_mode():
        context.configure(url=secret.get_secret_value(), literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(
        secret.get_secret_value(),
        poolclass=pool.NullPool,
        connect_args={"connect_timeout": settings.db_connect_timeout},
        hide_parameters=True,
    )
    try:
        if engine.dialect.name != "postgresql":
            raise RuntimeError("Resource foundation requires PostgreSQL")
        with engine.connect() as connection:
            configure(connection)
    finally:
        engine.dispose()


run()
