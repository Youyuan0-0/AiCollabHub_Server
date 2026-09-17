import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.session import create_db_engine


def test_postgresql_component_env_encodes_single_database_credentials(tmp_path):
    """分项 .env 生成单库连接，特殊密码正确编码且输出脱敏。"""
    path = tmp_path / "postgres.env"
    path.write_text(
        "APP_ENV=test\nAUTH_MODE=mock\nAUTH_MOCK_SECRET=" + "s" * 32 + "\n"
        "POSTGRES_HOST=database.example\nPOSTGRES_PORT=15433\nPOSTGRES_DB=platform\n"
        "POSTGRES_USER=writer\nPOSTGRES_PASSWORD='p@ss:/%word'\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=path)
    database = make_url(settings.database_url.get_secret_value())
    assert (database.host, database.port, database.password) == (
        "database.example",
        15433,
        "p@ss:/%word",
    )
    assert "p@ss:/%word" not in repr(settings) + settings.model_dump_json()


def test_explicit_url_overrides_component_settings():
    """已有完整 URL 优先，不因同时存在分项配置而切换目标或误报缺少字段。"""
    settings = Settings(database_url="sqlite:///chosen.db", postgres_host="unused")
    assert settings.database_url.get_secret_value() == "sqlite:///chosen.db"
    # 未提供显式 URL 时，只有密码也属于不完整配置，不能静默关闭连接。
    with pytest.raises(ValidationError, match="POSTGRES_HOST"):
        Settings(postgres_password="compose-only")
    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        Settings(postgres_host="incomplete")


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
        assert "-c idle_session_timeout=120s" in captured["options"]
        assert captured["options"].endswith("-c synchronous_commit=local")
    finally:
        engine.dispose()


def test_database_connection_identifies_machine_and_process_and_can_disable_idle_timeout(
    monkeypatch,
):
    """程序名前缀过长时仍保留主机和 PID，0 超时明确覆盖 URL 中的旧设置。"""
    captured = {}

    def connect(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("connection inspected")

    monkeypatch.setattr("psycopg.connect", connect)
    monkeypatch.setattr("app.db.session.socket.gethostname", lambda: "dev-machine")
    monkeypatch.setattr("app.db.session.os.getpid", lambda: 12345)
    engine = create_db_engine(
        "postgresql://user:password@localhost/platform"
        "?options=-c%20idle_session_timeout%3D10s&application_name=legacy",
        idle_session_timeout=0,
        application_name="feedback-" * 20,
    )
    try:
        with pytest.raises(RuntimeError, match="connection inspected"):
            engine.connect()
        name = captured["application_name"]
        assert len(name.encode("utf-8")) == 63
        assert name.startswith("feedback-") and name.endswith(":dev-machine:12345")
        assert captured["options"].endswith(
            "-c idle_session_timeout=0s -c synchronous_commit=local"
        )
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
        {"db_idle_session_timeout": -1},
        {"db_idle_session_timeout": 86401},
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
