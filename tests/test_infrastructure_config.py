import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
