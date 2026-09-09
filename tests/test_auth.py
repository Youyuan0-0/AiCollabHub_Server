from fastapi import Depends, Request
from fastapi.testclient import TestClient

from app.core.auth import Principal, require_permissions
from app.core.config import Settings
from app.main import create_app


class StubIdentityProvider:
    def __init__(self, permissions: list[str]):
        self.permissions = permissions

    async def authenticate(self, request: Request) -> Principal:
        return Principal(id="test-user", display_name="Test user", permissions=self.permissions)

    async def login_url(self, return_to: str) -> str:
        return "https://identity.example/login"


def test_unconfigured_identity_rejects_client_claims():
    with TestClient(create_app(Settings())) as client:
        response = client.get(
            "/api/v1/auth/session",
            headers={"X-Role": "admin", "X-User": "admin", "Authorization": "Bearer demo"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication_required"}


def test_session_uses_verified_provider_and_camel_case_contract():
    app = create_app(Settings(), identity_provider=StubIdentityProvider(["resource:publish"]))
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/session").json() == {
            "id": "test-user",
            "displayName": "Test user",
            "permissions": ["resource:publish"],
        }


def test_permission_dependency_denies_and_allows_explicit_grants():
    for permissions, expected in [([], 403), (["resource:review"], 200)]:
        app = create_app(Settings(), identity_provider=StubIdentityProvider(permissions))

        @app.get("/test-protected", dependencies=[Depends(require_permissions("resource:review"))])
        def protected():
            return {"ok": True}

        with TestClient(app) as client:
            assert client.get("/test-protected").status_code == expected


def test_login_is_not_faked_when_sso_is_unconfigured():
    with TestClient(create_app(Settings())) as client:
        assert client.get("/api/v1/auth/login-url").status_code == 503


def test_login_rejects_external_return_paths():
    app = create_app(Settings(), identity_provider=StubIdentityProvider([]))
    with TestClient(app) as client:
        for path in ("https://evil.example", "//evil.example", "/\\evil.example", "/\nevil"):
            assert (
                client.get("/api/v1/auth/login-url", params={"returnTo": path}).status_code == 422
            )
        assert (
            client.get(
                "/api/v1/auth/login-url", params={"returnTo": "/models?q=engine"}
            ).status_code
            == 200
        )
