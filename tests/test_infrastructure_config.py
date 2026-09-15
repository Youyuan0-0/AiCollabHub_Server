import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


def test_postgresql_component_env_builds_primary_and_replica(tmp_path):
    """使用与部署一致的分项 .env，确认主备地址独立、特殊密码正确编码且输出脱敏。"""
    path = tmp_path / "postgres.env"
    path.write_text(
        "APP_ENV=test\nAUTH_MODE=mock\nAUTH_MOCK_SECRET=" + "s" * 32 + "\n"
        "POSTGRES_HOST=primary.example\nPOSTGRES_PORT=15433\nPOSTGRES_DB=platform\n"
        "POSTGRES_USER=writer\nPOSTGRES_PASSWORD='p@ss:/%word'\n"
        "POSTGRES_READ_HOST=replica.example\nPOSTGRES_READ_PORT=15434\n"
        "POSTGRES_READ_DB=platform\nPOSTGRES_READ_USER=reader\n"
        "POSTGRES_READ_PASSWORD='read@secret'\nDB_READ_ENABLED=true\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=path)
    primary = make_url(settings.database_url.get_secret_value())
    replica = make_url(settings.database_read_url.get_secret_value())
    assert (primary.host, primary.port, primary.password) == (
        "primary.example",
        15433,
        "p@ss:/%word",
    )
    assert (replica.host, replica.port, replica.username) == ("replica.example", 15434, "reader")
    assert "p@ss:/%word" not in repr(settings) + settings.model_dump_json()


def test_explicit_url_overrides_component_settings():
    """已有完整 URL 优先，不因同时存在分项配置而切换目标或误报缺少字段。"""
    settings = Settings(database_url="sqlite:///chosen.db", postgres_host="unused")
    assert settings.database_url.get_secret_value() == "sqlite:///chosen.db"
    assert Settings(postgres_password="compose-only").database_url is None
    with pytest.raises(ValidationError, match="POSTGRES requires"):
        Settings(postgres_host="incomplete")


def test_services_are_optional_and_replica_routing_is_off_by_default():
    settings = Settings()
    assert not settings.db_read_enabled
    assert not settings.db_read_fallback_enabled
    assert settings.redis_url is None
    assert settings.minio_endpoint is None


@pytest.mark.parametrize("extra", [{}, {"database_url": "sqlite:///primary.db"}])
def test_read_routing_requires_both_database_connections(extra):
    with pytest.raises(ValidationError, match="DB_READ_ENABLED requires"):
        Settings(db_read_enabled=True, **extra)


def test_read_fallback_requires_explicit_opt_in_and_its_own_connection():
    with pytest.raises(ValidationError, match="DB_READ_FALLBACK_ENABLED requires"):
        Settings(db_read_fallback_enabled=True)
    with pytest.raises(ValidationError, match="DB_READ_FALLBACK_ENABLED requires"):
        Settings(
            database_url="sqlite:///primary.db",
            database_read_url="sqlite:///replica.db",
            db_read_enabled=True,
            db_read_fallback_enabled=True,
        )


@pytest.mark.parametrize(
    "values",
    [
        {"db_read_weight": -1},
        {"db_read_weight": 101},
        {"db_pool_size": 0},
        {"db_max_overflow": -1},
        {"redis_default_ttl": 0},
        {"redis_timeout": 0},
        {"minio_timeout": 0},
        {"minio_max_upload_bytes": 0},
    ],
)
def test_pool_timeout_and_size_limits_are_validated(values):
    with pytest.raises(ValidationError):
        Settings(**values)


def test_partial_storage_credentials_are_rejected():
    with pytest.raises(ValidationError, match="MinIO requires"):
        Settings(minio_endpoint="http://localhost:9000")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@storage.example",
        "http://localhost:9000/bucket",
        "http://localhost:9000?token=secret",
        "http://localhost:9000/#fragment",
    ],
)
def test_storage_endpoint_does_not_accept_credentials_or_paths(endpoint):
    with pytest.raises(ValidationError, match="MINIO_ENDPOINT must"):
        Settings(minio_endpoint=endpoint, minio_access_key="key", minio_secret_key="secret")


def test_service_credentials_are_redacted():
    settings = Settings(
        redis_url="redis://:cache-password@localhost:6379/0",
        database_read_url="postgresql+psycopg://user:replica-password@localhost/replica",
        database_migration_url="postgresql+psycopg://user:migration-password@localhost/primary",
        minio_endpoint="https://storage.example",
        minio_access_key="storage-access",
        minio_secret_key="storage-password",
    )
    serialized = repr(settings) + settings.model_dump_json()
    for value in (
        "cache-password",
        "replica-password",
        "migration-password",
        "storage-access",
        "storage-password",
    ):
        assert value not in serialized
