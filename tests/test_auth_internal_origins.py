"""验证容器内网认证地址与浏览器公网来源互不混用。"""

from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.mock_iam import PREFIX, create_mock_iam


def internal_settings(**overrides):
    values = {
        "_env_file": None,
        "app_env": "test",
        "auth_mode": "mock",
        "database_url": "sqlite://",
        "auth_mock_secret": "container-test-secret-at-least-32-characters",
        "auth_public_origin": "http://public.test",
        "auth_mock_iam_url": "http://public.test",
        "auth_internal_origin": "http://api:8000",
        "auth_mock_iam_internal_url": "http://mock-iam:18002",
    }
    return Settings(**(values | overrides))


def test_cas_redirects_remain_public_and_validation_uses_internal_origin():
    settings = internal_settings()
    cas = MockCasClient(settings)
    cas.client.close()
    callback = "http://public.test/api/v1/auth/callback?state=example"
    requests = []

    def validate(request):
        requests.append(request)
        return httpx.Response(
            200, json={"serviceResponse": {"authenticationSuccess": {"user": "mock-user"}}}
        )

    with httpx.Client(transport=httpx.MockTransport(validate)) as client:
        cas.client = client
        assert cas.login_url(callback).startswith("http://public.test" + PREFIX + "/login?")
        assert cas.logout_url("http://public.test/login").startswith(
            "http://public.test" + PREFIX + "/logout?"
        )
        assert cas.validate("ST-example", callback).subject == "mock-user"
    assert str(requests[0].url).startswith(
        "http://mock-iam:18002" + PREFIX + "/p3/serviceValidate?"
    )
    assert requests[0].url.params["service"] == callback


def test_iam_notifies_internal_api_and_checks_public_callbacks():
    settings = internal_settings()
    notifications = []

    def notify(request):
        notifications.append(request)
        return httpx.Response(200)

    callback = "http://public.test/api/v1/auth/callback?state=example"
    with (
        httpx.Client(transport=httpx.MockTransport(notify)) as notification_client,
        TestClient(
            create_mock_iam(settings, notification_client),
            base_url="http://public.test",
            follow_redirects=False,
        ) as iam,
    ):
        assert iam.get(PREFIX + "/login", params={"service": callback}).status_code == 200
        issued = iam.post(
            PREFIX + "/login",
            data={
                "service": callback,
                "subject": "mock-user",
                "challenge": iam.cookies.get("ach_mock_form"),
            },
            headers={"Origin": "http://public.test"},
        )
        assert issued.status_code == 303
        assert issued.headers["location"].startswith(callback + "&ticket=")
        # 即使服务间走内网，也不接受内网地址作为浏览器回调。
        rejected = iam.get(
            PREFIX + "/login",
            params={"service": "http://api:8000/api/v1/auth/callback?state=example"},
        )
        assert rejected.status_code == 400
        logged_out = iam.get(
            PREFIX + "/logout?" + urlencode({"service": "http://public.test/login"})
        )
        assert logged_out.status_code == 303
        assert logged_out.headers["location"] == "http://public.test/login"
    assert len(notifications) == 1
    assert str(notifications[0].url) == "http://api:8000/api/v1/auth/single-logout"
    assert (
        notifications[0].headers["x-mock-iam-key"] == settings.auth_mock_secret.get_secret_value()
    )


@pytest.mark.parametrize("field", ["auth_internal_origin", "auth_mock_iam_internal_url"])
@pytest.mark.parametrize("value", ["http://user:password@internal", "http://internal/path"])
def test_internal_origins_reject_credentials_and_paths(field, value):
    with pytest.raises(ValidationError, match="Authentication URLs must be origins"):
        internal_settings(**{field: value})


def test_empty_internal_origins_keep_existing_public_defaults():
    settings = internal_settings(auth_internal_origin="", auth_mock_iam_internal_url="")
    cas = MockCasClient(settings)
    try:
        assert settings.auth_internal_origin is None
        assert cas.validation_base == cas.base
    finally:
        cas.close()
