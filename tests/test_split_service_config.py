from pathlib import Path

import pytest
from dotenv import dotenv_values, set_key
from pydantic import ValidationError
from redis.connection import SSLConnection, parse_url
from sqlalchemy.engine import make_url

from app.core.config import Settings
from scripts.configure_services import main as import_services


@pytest.mark.parametrize(
    ("prefix", "target"),
    [
        ("POSTGRES", "database_url"),
        ("POSTGRES_READ", "database_read_url"),
        ("POSTGRES_READ_FALLBACK", "database_read_fallback_url"),
        ("POSTGRES_MIGRATION", "database_migration_url"),
    ],
)
def test_postgres_environment_preserves_credentials_and_masks_output(monkeypatch, prefix, target):
    password = "p@ss:/?#%+ word'\""
    for field, value in {
        "HOST": "::1",
        "PORT": "15433",
        "DB": "platform",
        "USER": "role@team",
        "PASSWORD": password,
    }.items():
        monkeypatch.setenv(f"{prefix}_{field}", value)
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "7")
    settings = Settings()
    url = make_url(getattr(settings, target).get_secret_value())
    assert (url.drivername, url.host, url.port, url.database) == (
        "postgresql+psycopg",
        "::1",
        15433,
        "platform",
    )
    assert (url.username, url.password, url.query["connect_timeout"]) == (
        "role@team",
        password,
        "7",
    )
    assert password not in repr(settings) + settings.model_dump_json()


def test_split_connections_enable_auth_and_keep_distinct_credentials():
    fields = {}
    for prefix in ("postgres", "postgres_read", "postgres_read_fallback", "postgres_migration"):
        fields.update(
            {
                f"{prefix}_host": prefix + ".example",
                f"{prefix}_db": "platform",
                f"{prefix}_user": prefix,
                f"{prefix}_password": prefix + "-secret",
            }
        )
    settings = Settings(
        **fields,
        db_read_enabled=True,
        db_read_fallback_enabled=True,
        auth_mode="mock",
        auth_mock_secret="s" * 48,
    )
    for prefix, target in (
        ("postgres", "database_url"),
        ("postgres_read", "database_read_url"),
        ("postgres_read_fallback", "database_read_fallback_url"),
        ("postgres_migration", "database_migration_url"),
    ):
        url = make_url(getattr(settings, target).get_secret_value())
        assert (url.username, url.password) == (prefix, prefix + "-secret")


def test_partial_standby_configuration_is_not_filled_with_primary_credentials():
    with pytest.raises(ValidationError, match="POSTGRES_READ_PASSWORD") as error:
        Settings(
            postgres_host="primary",
            postgres_db="platform",
            postgres_user="writer",
            postgres_password="private-writer-secret",
            postgres_read_host="standby",
            postgres_read_db="platform",
            postgres_read_user="reader",
        )
    assert "private-writer-secret" not in str(error.value)


@pytest.mark.parametrize("user", [None, "acl@user"])
@pytest.mark.parametrize("secure", [False, True])
def test_redis_client_receives_raw_password_for_password_only_and_acl(user, secure):
    password = "cache@:/?#%+ space'\""
    settings = Settings(
        redis_host="::1",
        redis_port=16379,
        redis_user=user,
        redis_password=password,
        redis_db=3,
        redis_ssl=secure,
    )
    connection = parse_url(settings.redis_url.get_secret_value())
    assert (connection["host"], connection["port"], connection["db"]) == ("::1", 16379, 3)
    assert connection.get("username") == user
    assert connection["password"] == password
    assert (connection.get("connection_class") is SSLConnection) is secure
    assert password not in repr(settings) + settings.model_dump_json()


def test_redis_without_auth_and_credentials_without_host():
    connection = parse_url(Settings(redis_host="localhost").redis_url.get_secret_value())
    assert "username" not in connection and "password" not in connection
    with pytest.raises(ValidationError, match="REDIS_HOST"):
        Settings(redis_password="secret")


@pytest.mark.parametrize(
    "values",
    [
        {"postgres_port": 0},
        {"postgres_read_port": 65536},
        {"postgres_read_fallback_port": -1},
        {"postgres_migration_port": 65536},
        {"redis_port": 65536},
        {"redis_db": -1},
    ],
)
def test_invalid_service_ports_and_database_numbers_fail_early(values):
    with pytest.raises(ValidationError):
        Settings(**values)


def test_explicit_urls_keep_precedence_over_split_dotenv(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "POSTGRES_HOST=cloud\nPOSTGRES_DB=platform\nPOSTGRES_USER=writer\n"
        "POSTGRES_PASSWORD=secret\nREDIS_HOST=cloud\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=env, database_url="sqlite:///test.db", redis_url="redis://localhost:6379/2"
    )
    assert settings.database_url.get_secret_value() == "sqlite:///test.db"
    assert settings.redis_url.get_secret_value() == "redis://localhost:6379/2"


def test_example_keeps_external_services_optional():
    template = Path(__file__).resolve().parents[1] / ".env.example"
    settings = Settings(_env_file=template)
    assert settings.database_url is None and settings.database_read_url is None
    assert settings.redis_url is None and settings.minio_endpoint is None


def test_service_import_replaces_stale_urls_and_preserves_unrelated_settings(
    tmp_path, monkeypatch, capsys
):
    source, output = tmp_path / "services.env", tmp_path / "app.env"
    source_values = {
        "POSTGRES_USER": "writer",
        "POSTGRES_PASSWORD": "postgres@:# secret",
        "POSTGRES_DB": "platform",
        "POSTGRES_A_PORT": "25433",
        "POSTGRES_B_PORT": "25434",
        "POSTGRES_READ_PORT": "25435",
        "POSTGRES_READ_USER": "reader",
        "POSTGRES_READ_PASSWORD": "reader-secret",
        "MINIO_ROOT_PASSWORD": "object-secret",
        "REDIS_PASSWORD": "redis@:# secret",
        "REDIS_USER": "cache-user",
    }
    for key, value in source_values.items():
        set_key(source, key, value)
    output.write_text(
        "DATABASE_URL=sqlite:///old.db\nDATABASE_READ_URL=sqlite:///old-read.db\n"
        "REDIS_URL=redis://old:6379/0\nAUTH_MODE=disabled\nAPP_ENV=test\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "configure_services",
            "--source-env",
            str(source),
            "--output-env",
            str(output),
            "--host",
            "services.example",
        ],
    )
    import_services()
    values = dotenv_values(output)
    assert not {"DATABASE_URL", "DATABASE_READ_URL", "REDIS_URL"} & values.keys()
    settings = Settings(_env_file=output)
    assert settings.app_env == "test" and settings.auth_mode == "disabled"
    primary = make_url(settings.database_url.get_secret_value())
    standby = make_url(settings.database_read_url.get_secret_value())
    assert (primary.username, primary.password, primary.port) == (
        "writer",
        source_values["POSTGRES_PASSWORD"],
        25433,
    )
    assert (standby.username, standby.password, standby.port) == ("reader", "reader-secret", 25435)
    assert (
        parse_url(settings.redis_url.get_secret_value())["password"]
        == source_values["REDIS_PASSWORD"]
    )
    assert dotenv_values(source) == source_values
    captured = capsys.readouterr()
    for key, value in source_values.items():
        if "PASSWORD" in key:
            assert value not in captured.out + captured.err
