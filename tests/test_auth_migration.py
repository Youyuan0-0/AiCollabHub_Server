from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_auth_migration_preserves_existing_tables_and_roundtrips(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated_history (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO unrelated_history (id) VALUES (1)"))
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "migrations"))
    command.upgrade(config, "head")
    assert {"auth_session", "auth_login_request", "auth_event_log"}.issubset(
        inspect(engine).get_table_names()
    )
    command.check(config)
    command.downgrade(config, "20260913_01")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM unrelated_history")) == 1
    assert "auth_session" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    engine.dispose()
