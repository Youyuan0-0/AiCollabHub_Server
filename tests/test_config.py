import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_development_does_not_select_a_database():
    settings = Settings()
    assert settings.app_env == "development"
    assert settings.database_url is None


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_database_url_is_unconfigured(value):
    assert Settings(database_url=value).database_url is None


def test_production_requires_explicit_database():
    with pytest.raises(ValidationError, match="DATABASE_URL is required"):
        Settings(app_env="production")


def test_database_credentials_are_redacted():
    settings = Settings(database_url="postgresql://user:secret-password@localhost/platform")
    assert "secret-password" not in repr(settings)
    assert "secret-password" not in settings.model_dump_json()


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Configured API")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:5173"]')
    settings = Settings()
    assert settings.app_name == "Configured API"
    assert settings.cors_origins == ["http://localhost:5173"]
