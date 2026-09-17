from pathlib import Path
from shutil import copytree

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Integer, MetaData, String, Table, func, inspect, select

from app.db.base import Base
from app.db.session import create_db_engine, get_session


def test_session_commits_explicitly_and_rolls_back_uncommitted_work(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'sessions.db').as_posix()}")
    metadata = MetaData()
    records = Table(
        "records", metadata, Column("id", Integer, primary_key=True), Column("name", String)
    )
    metadata.create_all(engine)
    try:
        # 直接驱动依赖生成器，模拟请求提交、未提交和业务异常三条会话清理路径。
        dependency = get_session(engine)
        session = next(dependency)
        session.execute(records.insert().values(name="committed"))
        session.commit()
        dependency.close()

        dependency = get_session(engine)
        session = next(dependency)
        session.execute(records.insert().values(name="uncommitted"))
        dependency.close()

        dependency = get_session(engine)
        session = next(dependency)
        session.execute(records.insert().values(name="failed"))
        with pytest.raises(RuntimeError, match="service failure"):
            dependency.throw(RuntimeError("service failure"))

        with engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(records)) == 1
            assert connection.scalar(select(records.c.name)) == "committed"
    finally:
        engine.dispose()


def migration_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    return Config(str(root / "alembic.ini"))


def test_migrations_require_database_configuration():
    with pytest.raises(RuntimeError, match="DATABASE_URL or DATABASE_MIGRATION_URL"):
        command.upgrade(migration_config(), "head")


def test_migration_environment_runs_against_real_database(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(migration_config(), "head")
    command.current(migration_config())
    engine = create_db_engine(url)
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_generated_migration_upgrades_and_downgrades(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    # 在临时目录生成迁移文件，避免测试修改项目中的正式迁移历史。
    scripts = copytree(root / "migrations", tmp_path / "migrations")
    config = migration_config()
    config.set_main_option("script_location", str(scripts))
    url = f"sqlite:///{(tmp_path / 'round-trip.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    table = Table(
        "migration_probe",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(64), nullable=False),
    )
    engine = create_db_engine(url)
    try:
        command.upgrade(config, "head")
        command.revision(config, message="migration smoke test", autogenerate=True)
        command.upgrade(config, "head")
        assert "migration_probe" in inspect(engine).get_table_names()
        # 只回退本测试生成的迁移；历史认证合并需要通过备份恢复。
        command.downgrade(config, "-1")
        assert "migration_probe" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        # 测试表注册在共享元数据中，需要移除，避免影响后续用例。
        Base.metadata.remove(table)


def test_migration_connection_can_be_separate_from_runtime_connection(tmp_path, monkeypatch):
    primary = tmp_path / "runtime.db"
    migration = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{primary.as_posix()}")
    monkeypatch.setenv("DATABASE_MIGRATION_URL", f"sqlite:///{migration.as_posix()}")
    command.upgrade(migration_config(), "head")
    engine = create_db_engine(f"sqlite:///{migration.as_posix()}")
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
        assert not primary.exists()
    finally:
        engine.dispose()
