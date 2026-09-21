"""验证数据集独立迁移与全量迁移兼容，不接触开发数据库。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.modules.datasets.repository import DatasetRepository

ROOT = Path(__file__).resolve().parents[1]


def migration_config(url):
    """向独立迁移注入临时库，避免读取开发环境凭据。"""
    config = Config(str(ROOT / "alembic.datasets.ini"))
    config.attributes["settings"] = Settings(database_url=url)
    return config


def test_independent_migration_preserves_unknown_global_revision(tmp_path):
    """全局版本不属于当前代码时仍可建表，重复执行保留记录及全局版本。"""
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    engine = create_engine(url)
    config = migration_config(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('20260917_01')"))
            connection.execute(text("CREATE TABLE unrelated_probe (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO unrelated_probe VALUES (7)"))
        command.upgrade(config, "head")
        saved = DatasetRepository(engine).save("publication:probe", {"name": "保留记录"}, None)
        command.upgrade(config, "head")
        command.check(config)
        assert DatasetRepository(engine).get("publication:probe") == saved
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260917_01"
            )
            assert connection.scalar(text("SELECT id FROM unrelated_probe")) == 7
    finally:
        engine.dispose()


@pytest.mark.parametrize("module_first", [False, True])
def test_full_and_module_migrations_keep_existing_records(tmp_path, monkeypatch, module_first):
    """两种部署顺序均不得重复建表或覆盖已有业务记录。"""
    url = f"sqlite:///{tmp_path / 'aligned.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    full = Config(str(ROOT / "alembic.ini"))
    module = migration_config(url)
    first, second = (module, full) if module_first else (full, module)
    engine = create_engine(url)
    try:
        command.upgrade(first, "head")
        saved = DatasetRepository(engine).save("activity:probe", {"rating": 5}, None)
        command.upgrade(second, "head")
        assert DatasetRepository(engine).get("activity:probe") == saved
    finally:
        engine.dispose()


def test_incompatible_existing_table_is_not_adopted(tmp_path):
    """同名表缺少必需字段时中止迁移，不能登记为成功。"""
    url = f"sqlite:///{tmp_path / 'invalid.db'}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE dataset_records (key VARCHAR(240) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO dataset_records VALUES ('keep')"))
        with pytest.raises(RuntimeError, match="schema does not match"):
            command.upgrade(migration_config(url), "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT key FROM dataset_records")) == "keep"
    finally:
        engine.dispose()


@pytest.mark.parametrize("starting_revision", ["20260916_02", "20260917_auth01"])
def test_merge_head_preserves_both_branch_data(tmp_path, monkeypatch, starting_revision):
    """任一已部署分支都可升级到唯一 head，已有数据集和其他模块记录不会丢失。"""
    url = f"sqlite:///{tmp_path / 'branches.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    current_head = ScriptDirectory.from_config(config).get_current_head()
    assert current_head is not None
    engine = create_engine(url)
    try:
        command.upgrade(config, starting_revision)
        command.upgrade(migration_config(url), "head")
        saved = DatasetRepository(engine).save(
            "publication:existing", {"name": "保留已发布申请"}, None
        )
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE unrelated_probe (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO unrelated_probe VALUES (7)"))
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        assert DatasetRepository(engine).get("publication:existing") == saved
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == current_head
            )
            assert connection.scalar(text("SELECT id FROM unrelated_probe")) == 7
    finally:
        engine.dispose()
