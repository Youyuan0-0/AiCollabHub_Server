import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.db.session import create_db_engine


def test_database_connection_uses_writable_target_and_local_durability(monkeypatch):
    captured = {}

    def connect(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("connection inspected")

    monkeypatch.setattr("psycopg.connect", connect)
    engine = create_db_engine(
        "postgresql+psycopg://user:password@localhost/platform"
        "?options=-c%20search_path%3Disolated%20-c%20synchronous_commit%3Doff"
        "&target_session_attrs=any"
    )
    try:
        with pytest.raises(RuntimeError, match="connection inspected"):
            engine.connect()
        assert captured["target_session_attrs"] == "read-write"
        assert "search_path=isolated" in captured["options"]
        assert captured["options"].endswith("-c synchronous_commit=local")
    finally:
        engine.dispose()


def test_services_are_optional():
    settings = Settings()
    assert settings.database_url is None
    assert settings.redis_url is None
    assert settings.minio_endpoint is None


@pytest.mark.parametrize(
    "values",
    [
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
        database_url="postgresql+psycopg://user:database-password@localhost/platform",
        database_migration_url="postgresql+psycopg://user:migration-password@localhost/primary",
        minio_endpoint="https://storage.example",
        minio_access_key="storage-access",
        minio_secret_key="storage-password",
    )
    serialized = repr(settings) + settings.model_dump_json()
    for value in (
        "cache-password",
        "database-password",
        "migration-password",
        "storage-access",
        "storage-password",
    ):
        assert value not in serialized
