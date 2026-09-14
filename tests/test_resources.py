from contextlib import ExitStack
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from redis import Redis
from sqlalchemy.engine import Engine

from app.cache.client import CacheDependency
from app.core.config import Settings
from app.core.resources import ApplicationResources, create_resources
from app.db.routing import DatabasePools, ReadSessionDependency
from app.db.session import SessionDependency, create_db_engine
from app.main import create_app
from app.storage.client import StorageDependency, StorageUnavailable


def test_resources_close_on_shutdown_and_state_is_cleared():
    callbacks = ExitStack()
    close = Mock()
    callbacks.callback(close)
    resources = ApplicationResources(_cleanup=callbacks)
    app = create_app(Settings(), resources_factory=lambda settings: resources)
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert app.state.resources is resources
    close.assert_called_once()
    assert app.state.resources is None and app.state.database_engine is None


def test_real_client_factories_and_liveness_do_not_connect_to_services(monkeypatch):
    connect = Mock(side_effect=AssertionError("Startup must not contact external services"))
    monkeypatch.setattr(Engine, "connect", connect)
    monkeypatch.setattr(Redis, "execute_command", connect)
    monkeypatch.setattr(Minio, "_execute", connect)
    settings = Settings(
        database_url="postgresql+psycopg://writer:placeholder@127.0.0.1/platform",
        database_read_url="postgresql+psycopg://reader:placeholder@127.0.0.1/platform",
        db_read_enabled=True,
        redis_url="redis://127.0.0.1:6379/0",
        minio_endpoint="http://127.0.0.1:9000",
        minio_access_key="placeholder-access",
        minio_secret_key="placeholder-secret",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").json() == {"status": "ok"}
        assert app.state.resources.database.replica is not None
        assert app.state.resources.cache is not None
        assert app.state.resources.storage is not None
    connect.assert_not_called()


def test_partial_resource_initialization_cleans_up_previous_clients(monkeypatch):
    database, cache = Mock(), Mock()
    monkeypatch.setattr("app.core.resources.DatabasePools.from_settings", lambda settings: database)
    monkeypatch.setattr("app.core.resources.RedisCache.from_settings", lambda settings: cache)
    monkeypatch.setattr(
        "app.core.resources.MinioStorage.from_settings",
        Mock(side_effect=ValueError("invalid storage configuration")),
    )
    settings = Settings(
        database_url="sqlite:///primary.db",
        redis_url="redis://localhost:6379",
        minio_endpoint="http://localhost:9000",
        minio_access_key="key",
        minio_secret_key="secret",
    )
    with pytest.raises(ValueError, match="invalid storage"):
        create_resources(settings)
    cache.close.assert_called_once()
    database.close.assert_called_once()


def test_close_failure_still_closes_other_resources_and_clears_state():
    cleanup = ExitStack()
    final_close = Mock()
    cleanup.callback(final_close)
    cleanup.callback(Mock(side_effect=RuntimeError("close failed")))
    resources = ApplicationResources(_cleanup=cleanup)
    app = create_app(Settings(), resources_factory=lambda settings: resources)
    with pytest.raises(RuntimeError, match="close failed"), TestClient(app):
        pass
    final_close.assert_called_once()
    assert app.state.resources is None


def test_unconfigured_dependencies_have_explicit_behavior():
    app = create_app(Settings())

    @app.get("/test-cache")
    def cache_probe(cache: CacheDependency):
        return {"configured": cache is not None}

    @app.get("/test-storage")
    def storage_probe(storage: StorageDependency):
        return {"configured": True}

    @app.get("/test-read")
    def read_probe(session: ReadSessionDependency):
        return {"configured": True}

    with TestClient(app) as client:
        assert client.get("/test-cache").json() == {"configured": False}
        assert client.get("/test-storage").json() == {"detail": "object_storage_not_configured"}
        assert client.get("/test-read").json() == {"detail": "database_not_configured"}
        assert (
            client.get("/api/v1/health/dependencies").json()["dependencies"]["redis"]
            == "not_configured"
        )


def test_dependency_report_is_separate_from_primary_readiness(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'primary.db').as_posix()}")
    cache = Mock()
    cache.probe.return_value = False
    storage = Mock()
    storage.probe.return_value = True
    resources = ApplicationResources(database=DatabasePools(engine), cache=cache, storage=storage)
    resources._cleanup.callback(engine.dispose)
    app = create_app(
        Settings(),
        resources_factory=lambda settings: resources,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").json() == {"status": "ready"}
        report = client.get("/api/v1/health/dependencies")
        assert report.status_code == 200
        assert report.json() == {
            "status": "degraded",
            "dependencies": {
                "database": "available",
                "replica": "disabled",
                "read_fallback": "disabled",
                "redis": "unavailable",
                "minio": "available",
            },
        }


def test_production_does_not_expose_development_diagnostics(tmp_path):
    cache = Mock()
    resources = ApplicationResources(cache=cache)
    app = create_app(
        Settings(app_env="production", database_url=f"sqlite:///{tmp_path / 'prod.db'}"),
        resources_factory=lambda settings: resources,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/health/dependencies")
        assert response.status_code == 404
    cache.probe.assert_not_called()


def test_unavailable_read_database_returns_503_without_automatic_fallback(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'primary.db').as_posix()}",
        database_read_url=f"sqlite:///{(tmp_path / 'missing' / 'replica.db').as_posix()}",
        db_read_enabled=True,
    )
    app = create_app(settings)

    @app.get("/test-read")
    def read_probe(session: ReadSessionDependency):
        return {"configured": True}

    with TestClient(app) as client:
        response = client.get("/test-read")
        assert response.status_code == 503
        assert response.json() == {"detail": "read_database_unavailable"}


def test_storage_error_response_does_not_leak_sdk_details():
    app = create_app(Settings())

    @app.get("/test-storage-error")
    def storage_error():
        raise StorageUnavailable("private-url password=secret")

    with TestClient(app) as client:
        response = client.get("/test-storage-error")
        assert response.status_code == 503
        assert response.json() == {"detail": "object_storage_unavailable"}


def test_fastapi_primary_and_read_dependencies_use_separate_databases(tmp_path):
    from sqlalchemy import Column, Integer, MetaData, String, Table, select

    metadata = MetaData()
    table = Table(
        "records", metadata, Column("id", Integer, primary_key=True), Column("label", String)
    )
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'primary.db').as_posix()}",
        database_read_url=f"sqlite:///{(tmp_path / 'replica.db').as_posix()}",
        db_read_enabled=True,
    )
    app = create_app(settings)

    @app.get("/test-primary")
    def primary_probe(session: SessionDependency):
        return {"label": session.scalar(select(table.c.label))}

    @app.get("/test-replica")
    def replica_probe(session: ReadSessionDependency):
        return {"label": session.scalar(select(table.c.label))}

    with TestClient(app) as client:
        pools = app.state.resources.database
        for label, engine in (("primary", pools.primary), ("replica", pools.replica)):
            metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(table.insert().values(id=1, label=label))
        assert client.get("/test-primary").json() == {"label": "primary"}
        assert client.get("/test-replica").json() == {"label": "replica"}
