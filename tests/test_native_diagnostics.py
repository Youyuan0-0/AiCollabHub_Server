"""验证离线诊断能区分认证阶段、保护凭据并在真实 HTTP 异常下继续取证。"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[1] / "deploy/native/diagnostics"


@pytest.fixture
def diagnostic(monkeypatch):
    monkeypatch.syspath_prepend(str(DIRECTORY.parent))
    monkeypatch.syspath_prepend(str(DIRECTORY))
    spec = importlib.util.spec_from_file_location("diagnostic_test", DIRECTORY / "diagnose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_redacts_nested_credentials_and_encoded_ticket(diagnostic):
    from core import redact, safe_url

    sample = 'Cookie: session=private-cookie\nhttps://name:private-pass@site/cb?ticket=private-ticket&service=https%3A%2F%2Fsite%2F%3Fstate%3Dprivate-state\npassword="private-other"\nknown=private-env'
    assert "private-" not in redact(sample, ("private-env",))
    assert "private-" not in safe_url(
        "https://name:private-pass@site/path?ticket=private-ticket#private-fragment"
    )
    assert "ticket_present=1" in redact("ticket_present=1 state_present=0")


def test_flows_do_not_mix_users_or_probes(diagnostic):
    from core import summarize_flows

    events = [
        {"stage": "login", "result": "redirect_to_iam", "request_id": "a", "flow_id": "flow-a"},
        {"stage": "callback", "result": "received", "request_id": "b", "flow_id": "flow-b"},
        {"stage": "cas_validate", "result": "success", "request_id": "b"},
        {
            "stage": "callback",
            "result": "session_created",
            "request_id": "b",
            "flow_id": "flow-b",
            "session_ref": "session-b",
        },
        {"stage": "session", "result": "accepted", "request_id": "c", "session_ref": "session-b"},
        {
            "stage": "login",
            "result": "redirect_to_iam",
            "request_id": "p",
            "flow_id": "probe",
            "probe": "1",
        },
    ]
    flows = {row["flowId"]: row for row in summarize_flows(events)}
    assert set(flows) == {"flow-a", "flow-b"}
    assert flows["flow-a"]["steps"][1]["status"] == "NOT_OBSERVED"
    assert flows["flow-b"]["steps"][-1]["status"] == "PASS"
    assert flows["flow-a"]["steps"][-1]["status"] != "PASS"


def test_validation_failure_does_not_claim_later_stages_ran(diagnostic):
    from core import summarize_flows

    events = [
        {"stage": "callback", "result": "received", "request_id": "a", "flow_id": "flow-a"},
        {"stage": "cas_validate", "result": "failed", "reason": "timeout", "request_id": "a"},
    ]
    flow = summarize_flows(events)[0]
    assert flow["steps"][3]["status"] == "FAIL"
    assert flow["steps"][4]["status"] == "NOT_EXECUTED"
    assert "超时" in flow["conclusion"]


def test_binding_failure_is_not_reported_as_iam_outage(diagnostic):
    from core import summarize_flows

    flow = summarize_flows(
        [
            {"stage": "callback", "result": "received", "request_id": "a", "flow_id": "flow"},
            {
                "stage": "login",
                "result": "failed",
                "reason": "login_request_expired",
                "request_id": "a",
                "flow_id": "flow",
            },
        ]
    )[0]
    assert flow["steps"][2]["status"] == "FAIL"
    assert flow["steps"][3]["status"] == "NOT_EXECUTED"


def test_home_callback_has_its_own_stage_without_claiming_ticket_validation(diagnostic):
    from core import summarize_flows

    events = [
        {"stage": "login", "result": "redirect_to_iam", "request_id": "a", "flow_id": "flow"},
        {"stage": "home_callback", "result": "forwarded", "request_id": "b", "flow_id": "flow"},
    ]
    flow = summarize_flows(events)[0]
    assert flow["steps"][1]["stage"] == "home_callback"
    assert flow["steps"][1]["status"] == "PASS"
    assert flow["steps"][2]["stage"] == "callback"
    assert flow["steps"][2]["status"] == "NOT_OBSERVED"
    assert flow["steps"][4]["status"] == "NOT_OBSERVED"
    events[1].update(result="rejected", reason="invalid_callback_parameters")
    rejected = summarize_flows(events)[0]
    assert rejected["steps"][1]["status"] == "FAIL"
    assert rejected["steps"][2]["status"] == "NOT_EXECUTED"
    assert "首页登录参数" in rejected["conclusion"]


@pytest.fixture
def http_service():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/login":
                self.send_response(303)
                self.send_header(
                    "Location",
                    "http://example.test/iam?ticket=private-ticket&service=https%3A%2F%2Fexample.test%2Fapi%2Fv1%2Fauth%2Fcallback%3Fstate%3Dprivate-state%26scope%3Duser",
                )
                self.send_header(
                    "Set-Cookie", "ach_login_user=private-cookie; Secure; HttpOnly; Path=/api"
                )
                self.end_headers()
            elif self.path == "/bad-json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"not-json-private-body")
            elif self.path == "/html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>private-iam-login-page</h1>")
            else:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"detail":"authentication_required","csrfToken":"private-csrf"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield "http://127.0.0.1:" + str(server.server_port)
    server.shutdown()
    server.server_close()


def test_real_http_probe_does_not_follow_or_export_secrets(diagnostic, http_service):
    from core import http_probe

    result = http_probe(http_service + "/login")
    assert result["status"] == 303
    assert result["servicePath"] == "/api/v1/auth/callback"
    assert result["cookieAttributes"][0]["secure"] is True
    assert "private-" not in json.dumps(result)
    assert http_probe(http_service + "/bad-json")["invalidJson"] is True
    assert http_probe(http_service + "/html")["contentType"] == "text/html"
    assert http_probe(http_service + "/session")["status"] == 401


def test_report_escapes_html_and_import_discards_unknown_fields(diagnostic, tmp_path):
    report = diagnostic.Diagnosis()
    report.secrets = ("private-secret",)
    source = tmp_path / "browser.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aih-browser-diagnostics-v1",
                "userAgent": "test",
                "cookies": "private-cookie",
                "events": [
                    {
                        "event": "xhr",
                        "status": 200,
                        "rawBody": "private-body",
                        "path": "/config?ticket=private-ticket",
                    }
                ],
            }
        )
    )
    report.browser(source)
    report.add("<script>alert(1)</script>", "FAIL", "normal", "password=private-secret")
    report.save(tmp_path)
    result = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<script>" not in result
    assert "private-" not in result
    import tarfile

    with tarfile.open(tmp_path / "diagnostics.tar.gz") as archive:
        assert set(archive.getnames()) == {"report.json", "report.html", "report.txt"}


def test_packaging_declares_diagnostics_without_instructions():
    source = (DIRECTORY.parents[2] / "scripts/package_native_release.py").read_text(
        encoding="utf-8"
    )
    assert '"instructions_included": False' in source
    assert 'bundle / "diagnostics"' in source


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema": "aih-browser-diagnostics-v1", "events": None, "userAgent": "test"},
        {"schema": "aih-browser-diagnostics-v1", "events": {}, "userAgent": "test"},
        {"schema": "aih-browser-diagnostics-v1", "events": [], "userAgent": {}},
    ],
)
def test_browser_report_rejects_wrong_shape(diagnostic, tmp_path, payload):
    source = tmp_path / "browser.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        diagnostic.Diagnosis().browser(source)


@pytest.mark.parametrize(
    "public,callback,secure,origin_status,cookie_status",
    [
        ("http", "http", "false", "PASS", "PASS"),
        ("http", "http", "true", "PASS", "FAIL"),
        ("https", "https", "true", "PASS", "PASS"),
        ("https", "https", "false", "PASS", "FAIL"),
        ("https", "http", "true", "FAIL", "PASS"),
    ],
)
def test_baseline_detects_cookie_protocol_conflicts(
    diagnostic, monkeypatch, tmp_path, public, callback, secure, origin_status, cookie_status
):
    config = {
        "AUTH_PUBLIC_ORIGIN": public + "://example.test",
        "AUTH_CALLBACK_ORIGIN": callback + "://example.test",
        "AUTH_COOKIE_SECURE": secure,
    }
    (tmp_path / "app-env.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "site.json").write_text(
        json.dumps({"public_origin": config["AUTH_PUBLIC_ORIGIN"]}), encoding="utf-8"
    )
    monkeypatch.setattr(diagnostic, "CONFIG", tmp_path)
    monkeypatch.setattr(diagnostic, "ROOT", tmp_path)
    monkeypatch.setattr(
        diagnostic, "command", lambda *a, **k: {"exitCode": 0, "stdout": "", "stderr": ""}
    )
    monkeypatch.setattr(diagnostic, "http_probe", lambda *a, **k: {})
    report = diagnostic.Diagnosis()
    report.baseline(network=False)
    checks = {row["name"]: row["status"] for row in report.report["checks"]}
    assert checks["网站与回调主机"] == origin_status
    assert checks["会话 Cookie 配置"] == cookie_status
