"""共享数据库的数据集迁移入口，不修改全局 alembic_version。"""

from alembic import context
from sqlalchemy import MetaData, create_engine, pool, text

from app.core.config import Settings
from app.db.session import normalize_database_url
from migrations.legacy_models import DatasetRecord

# Alembic 在 Windows 按系统编码读取 INI，中文说明集中放在本文件和模块 README。
# 命令行默认读取 .env；验证脚本可显式传入同一配置对象，避免误读运行目录的配置。
settings = context.config.attributes.get("settings") or Settings()
database_setting = settings.database_migration_url or settings.database_url
if database_setting is None:
    raise RuntimeError("Database configuration is required for dataset migrations")
url = normalize_database_url(database_setting.get_secret_value())
# 只比较本模块表，防止自动迁移接管认证、场景应用等其他模块的表。
metadata = MetaData()
for table in (DatasetRecord.__table__,):
    table.to_metadata(metadata)


def include_object(obj, name, object_type, reflected, compare_to):
    """独立迁移只管理数据集业务表，其他已存在的表不进入差异检查。"""
    return object_type != "table" or name in metadata.tables


def run_migrations():
    """使用同一数据库和独立版本表执行事务，提交仍等待本机 WAL 落盘。"""
    if context.is_offline_mode():
        raise RuntimeError("Dataset schema validation requires an online database connection")
    connect_args = {}
    if url.get_backend_name() == "postgresql":
        connect_args = {
            "connect_timeout": settings.db_connect_timeout,
            "target_session_attrs": "read-write",
            "options": (str(url.query.get("options", "")) + " -c synchronous_commit=local").strip(),
        }
    engine = create_engine(
        url, poolclass=pool.NullPool, hide_parameters=True, connect_args=connect_args
    )
    try:
        with engine.connect() as connection:
            # 显式事务使 PostgreSQL 的表变更、结构校验和模块版本写入一起提交。
            with connection.begin():
                if connection.dialect.name == "postgresql":
                    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
                    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                context.configure(
                    connection=connection,
                    target_metadata=metadata,
                    version_table="alembic_version_datasets",
                    compare_type=True,
                    include_object=include_object,
                )
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
