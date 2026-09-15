import pytest
from dotenv import dotenv_values, set_key

from app.core.config import Settings
from scripts import configure_mock_auth


def test_auth_initialization_preserves_services_and_is_idempotent(tmp_path):
    path = tmp_path / ".env"
    services = {
        "APP_ENV": "test",
        "POSTGRES_HOST": "database.example",
        "POSTGRES_USER": "writer",
        "POSTGRES_PASSWORD": "private@:/# secret",
        "POSTGRES_DB": "platform",
        "REDIS_HOST": "cache.example",
        "REDIS_PASSWORD": "cache-secret",
        "MINIO_ENDPOINT": "http://storage.example:9000",
        "MINIO_ACCESS_KEY": "app",
        "MINIO_SECRET_KEY": "storage-secret",
        "AUTH_SESSION_TTL": "3600",
        "AUTH_MOCK_ADMIN_SUBJECTS": '["mock-admin"]',
    }
    for key, value in services.items():
        set_key(path, key, value)

    configure_mock_auth.configure_mock_auth(path)
    first = path.read_bytes()
    configure_mock_auth.configure_mock_auth(path)
    assert path.read_bytes() == first
    values = dotenv_values(path)
    assert all(values[key] == value for key, value in services.items())
    settings = Settings(_env_file=path)
    assert settings.auth_mode == "mock"
    assert len(settings.auth_mock_secret.get_secret_value()) >= 32
    assert not (tmp_path / ".runtime").exists()


def test_existing_origins_and_secret_are_preserved_unless_ports_are_explicit(tmp_path):
    path = tmp_path / ".env"
    original = {
        "AUTH_PUBLIC_ORIGIN": "https://portal.example",
        "AUTH_MOCK_IAM_URL": "https://iam.example",
        "AUTH_MOCK_SECRET": "existing-secret-with-more-than-32-characters",
        "AUTH_COOKIE_SECURE": "true",
    }
    for key, value in original.items():
        set_key(path, key, value)
    configure_mock_auth.configure_mock_auth(path)
    assert all(dotenv_values(path)[key] == value for key, value in original.items())

    configure_mock_auth.configure_mock_auth(path, gateway_port=18881, iam_port=18003)
    updated = dotenv_values(path)
    assert updated["AUTH_PUBLIC_ORIGIN"] == "http://127.0.0.1:18881"
    assert updated["AUTH_MOCK_IAM_URL"] == "http://127.0.0.1:18003"
    assert updated["AUTH_COOKIE_SECURE"] == "false"
    assert updated["AUTH_MOCK_SECRET"] == original["AUTH_MOCK_SECRET"]


def test_auth_cli_uses_project_env_instead_of_working_directory(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(configure_mock_auth, "__file__", str(scripts / "configure_mock_auth.py"))
    monkeypatch.setattr("sys.argv", ["configure_mock_auth", "--gateway-port", "18881"])
    configure_mock_auth.main()
    values = dotenv_values(project / ".env")
    assert values["AUTH_PUBLIC_ORIGIN"] == "http://127.0.0.1:18881"
    assert not (tmp_path / ".env").exists()
    output = capsys.readouterr().out
    assert ".env" in output and values["AUTH_MOCK_SECRET"] not in output


def test_auth_initialization_rejects_production_without_changes(tmp_path):
    path = tmp_path / ".env"
    path.write_text("APP_ENV=production\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="prohibited in production"):
        configure_mock_auth.configure_mock_auth(path)
    assert path.read_bytes() == before
