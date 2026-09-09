from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_liveness_does_not_depend_on_database():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_readiness_reports_unconfigured_database():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "database_not_configured"}


def test_readiness_checks_real_connection(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'healthy.db').as_posix()}")
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready"}
    assert application.state.database_engine is None


def test_readiness_handles_database_failure_without_exposing_details(tmp_path):
    # 用不存在的父目录触发真实连接失败，无需依赖外部数据库服务。
    path = tmp_path / "missing-directory" / "unavailable.db"
    settings = Settings(database_url=f"sqlite:///{path.as_posix()}")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "database_unavailable"}
        assert "unavailable.db" not in response.text
        assert client.get("/api/v1/health/live").status_code == 200


def test_development_openapi():
    with TestClient(create_app(Settings())) as client:
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"] == "AiCollabHub API"
        assert "/api/v1/health/ready" in schema["paths"]


def test_production_disables_public_documentation(tmp_path):
    # SQLite 仅用于验证生产配置分支，不代表生产环境选用 SQLite。
    settings = Settings(
        app_env="production",
        database_url=f"sqlite:///{(tmp_path / 'test-only.db').as_posix()}",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_cors_is_opt_in():
    origin = "http://localhost:5173"
    headers = {"Origin": origin, "Access-Control-Request-Method": "GET"}
    with TestClient(create_app(Settings())) as client:
        response = client.options("/api/v1/health/live", headers=headers)
        assert "access-control-allow-origin" not in response.headers
    with TestClient(create_app(Settings(cors_origins=[origin]))) as client:
        response = client.options("/api/v1/health/live", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        response = client.options(
            "/api/v1/health/live",
            headers={"Origin": "http://untrusted.example", "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers
