"""以文档报文测试真实 CAS 客户端，不连接集团内网或开发数据库。"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.common.auth.cas import IAMUnavailable, Principal, TicketRejected
from app.modules.common.auth.iam_cas import IamCasClient

ORIGIN = "http://hfidaas-sso.aecc.cn"
APP_ID = "796872537136820224"
SERVICE = "http://platform.test:8086/api/v1/auth/callback?state=a%2Bb&scope=user"


def settings(**overrides):
    """只配置独立客户端所需地址，防止测试从本地环境读取连接凭据。"""
    return Settings(
        **{
            "_env_file": None,
            "auth_iam_origin": ORIGIN,
            "auth_iam_app_id": APP_ID,
            "auth_iam_admin_role_code": "platform_admin",
            **overrides,
        }
    )


def success(**attributes):
    """登录名与集团用户编号故意不同，防止误用顶层 user 作为关联键。"""
    return {
        "serviceResponse": {
            "authenticationSuccess": {
                "user": "login-account",
                "attributes": {"workNo": "employee-001", "displayName": "测试用户", **attributes},
            }
        }
    }


def xml_response(content):
    """组装 CAS 官方命名空间的最小响应。"""
    return (
        '<cas:serviceResponse xmlns:cas="http://www.yale.edu/tp/cas">'
        + content
        + "</cas:serviceResponse>"
    ).encode()


def validate_response(response):
    """执行正式验票和解析，仅用内存 HTTP 传输替换远端。"""
    client = IamCasClient(settings(), transport=httpx.MockTransport(lambda _: response))
    try:
        return client.validate("ST-local-test", SERVICE)
    finally:
        client.close()


def test_real_urls_preserve_callback_and_only_renew_when_requested():
    client = IamCasClient(settings())
    try:
        login = urlsplit(client.login_url(SERVICE))
        assert login.netloc == "hfidaas-sso.aecc.cn"
        assert login.path == f"/authn-api/v5/cas/{APP_ID}/login"
        assert parse_qs(login.query) == {"service": [SERVICE]}
        assert parse_qs(urlsplit(client.login_url(SERVICE, True)).query) == {
            "service": [SERVICE],
            "renew": ["true"],
        }
        logout = urlsplit(client.logout_url("http://platform.test/login?logged_out=1"))
        assert logout.path == f"/authn-api/v5/cas/{APP_ID}/logout"
        assert parse_qs(logout.query)["service"] == ["http://platform.test/login?logged_out=1"]
    finally:
        client.close()


def test_validate_is_unsigned_and_uses_work_no_and_original_service():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=success(roles=["platform_admin"]))

    client = IamCasClient(settings(), transport=httpx.MockTransport(respond))
    try:
        assert client.validate("ST-local-test", SERVICE) == Principal("employee-001", "测试用户")
    finally:
        client.close()
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/authn-api/v5/cas/{APP_ID}/p3/serviceValidate"
    assert dict(request.url.params) == {
        "ticket": "ST-local-test",
        "service": SERVICE,
        "format": "json",
    }
    assert "x-mock-iam-key" not in request.headers
    assert "authorization" not in request.headers


@pytest.mark.parametrize("name", ["", None])
def test_missing_display_name_uses_work_no(name):
    assert validate_response(httpx.Response(200, json=success(displayName=name))) == Principal(
        "employee-001", "employee-001"
    )


def test_xml_validation_uses_namespaced_attributes():
    body = xml_response(
        "<cas:authenticationSuccess><cas:user>login-account</cas:user><cas:attributes>"
        "<cas:workNo>employee-001</cas:workNo><cas:displayName>测试用户</cas:displayName>"
        "</cas:attributes></cas:authenticationSuccess>"
    )
    assert validate_response(httpx.Response(200, content=body)) == Principal(
        "employee-001", "测试用户"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            json={"serviceResponse": {"authenticationFailure": {"code": "INVALID_TICKET"}}},
        ),
        httpx.Response(
            200, content=xml_response('<cas:authenticationFailure code="INVALID_TICKET"/>')
        ),
    ],
)
def test_ticket_rejection_is_distinct_from_unavailable_service(response):
    with pytest.raises(TicketRejected):
        validate_response(response)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://another-server.test/"}),
        httpx.Response(500, text="upstream failed"),
        httpx.Response(200, text="<html>IAM login page</html>"),
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"serviceResponse": []}),
        httpx.Response(200, json={"serviceResponse": {"authenticationFailure": {}}}),
        httpx.Response(200, json=success(workNo=None)),
        httpx.Response(200, json=success(workNo="")),
        httpx.Response(200, json=success(workNo=" employee-001 ")),
        httpx.Response(200, json=success(workNo=1001)),
        httpx.Response(200, json=success(workNo="x" * 201)),
        httpx.Response(200, json=success(displayName=["ambiguous"])),
        httpx.Response(200, json=success(displayName="x" * 101)),
        httpx.Response(200, content=b" " * 65537),
        httpx.Response(
            200,
            json={
                "serviceResponse": {
                    **success()["serviceResponse"],
                    "authenticationFailure": {"code": "INVALID_TICKET"},
                }
            },
        ),
        httpx.Response(
            200,
            content=xml_response(
                "<cas:authenticationSuccess><cas:attributes>"
                "<cas:workNo>first</cas:workNo><cas:workNo>second</cas:workNo>"
                "</cas:attributes></cas:authenticationSuccess>"
            ),
        ),
        httpx.Response(
            200,
            content=b'<!DOCTYPE serviceResponse [<!ENTITY x "private">]>'
            + xml_response("<cas:authenticationSuccess>&x;</cas:authenticationSuccess>"),
        ),
    ],
)
def test_invalid_http_or_identity_response_never_creates_a_principal(response):
    with pytest.raises(IAMUnavailable):
        validate_response(response)


def test_validation_timeout_is_not_retried_and_error_does_not_expose_ticket():
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout(str(request.url), request=request)

    client = IamCasClient(settings(), transport=httpx.MockTransport(fail))
    try:
        with pytest.raises(IAMUnavailable) as error:
            client.validate("ST-private-value", SERVICE)
        assert "ST-private-value" not in str(error.value)
        assert error.value.__suppress_context__
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    "response,reason",
    [
        (httpx.Response(503), "http_status"),
        (httpx.Response(200, json=success(workNo=None)), "missing_work_no"),
        (httpx.Response(200, text="not JSON"), "invalid_response"),
    ],
)
def test_validation_log_identifies_stage_without_identity_or_ticket(caplog, response, reason):
    caplog.set_level("INFO", logger="app.modules.common.auth.iam_cas")
    with pytest.raises(IAMUnavailable):
        validate_response(response)
    logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "app.modules.common.auth.iam_cas"
    )
    assert "stage=cas_validate result=started" in logs
    assert "reason=" + reason in logs
    assert "duration_ms=" in logs
    assert "ST-local-test" not in logs
    assert "employee-001" not in logs
    assert SERVICE not in logs


@pytest.mark.parametrize("app_id", ["../other", "bad?query", "with space", "x" * 129])
def test_app_id_cannot_change_endpoint_path(app_id):
    with pytest.raises(ValidationError):
        settings(auth_iam_app_id=app_id)


def test_sync_source_ranges_are_validated_and_empty_by_default():
    assert settings().auth_iam_sync_allowed_networks == []
    allowed = settings(auth_iam_sync_allowed_networks=["192.0.2.10/32", "2001:db8::/64"])
    assert [str(item) for item in allowed.auth_iam_sync_allowed_networks] == [
        "192.0.2.10/32",
        "2001:db8::/64",
    ]
    with pytest.raises(ValidationError):
        settings(auth_iam_sync_allowed_networks=["hfidaas-sso.aecc.cn"])
