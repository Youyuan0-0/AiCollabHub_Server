"""工具连接配置与真实协议适配回归；供应方全部使用内存 HTTP 传输。"""

import json
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_toolchain_configuration import PATH, application, payload

from app.core.config import Settings
from app.modules.tools.toolchain import BASE


def supplier_tool(**changes):
    """只使用 Agent Dev P37–46 定义的供应方字段，包含不得返回浏览器的配置。"""
    return {
        "code": "external-tool-1",
        "version": "1767939302512",
        "workspaceCode": "tools-workspace",
        "name": "工具成果",
        "desc": "供应方公开说明",
        "toolType": "api",
        "serviceConfig": {"toolApiConfig": {"apiHeaderList": [{"fieldValue": "private-token"}]}},
        **changes,
    }


@pytest.fixture
def connected(monkeypatch):
    """通过实际装配和 POP 签名发起请求，仅将最底层 HTTP 传输换为本地响应。"""
    state = {"rows": [supplier_tool()], "calls": [], "detail": None, "failure": False}

    def upstream(_transport, request):
        state["calls"].append(request)
        if state["failure"]:
            raise httpx.ConnectError("offline", request=request)
        assert request.url.host == "tools.invalid"
        assert request.headers["authorization"].startswith("acs tools-ak:")
        assert request.headers["x-sfm-workspacecode"] == "tools-workspace"
        assert request.headers["x-sfm-authorization"] == "Bearer tools-app-key"
        body = json.loads(request.content)
        if request.url.path == BASE + "/pageQuery":
            assert request.headers["x-acs-action"] == "PostAgentOpenapiToolPagequery"
            assert body["toolCategory"] == "my"
            assert body["toolType"] == "api"
            data = {"list": state["rows"], "total": len(state["rows"])}
        else:
            assert request.url.path == BASE + "/getOne"
            assert request.headers["x-acs-action"] == "PostAgentOpenapiToolGetone"
            assert body == {"uniqueCode": "external-tool-1", "version": "1767939302512"}
            data = state["detail"] if state["detail"] is not None else state["rows"][0]
        return httpx.Response(200, json={"success": True, "data": data})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", upstream)
    settings = Settings(
        toolchain_tools_mode="live",
        toolchain_tools_types=["api"],
        toolchain_tool_entry_url="https://workbench.invalid/tools",
        toolchain_pop_url="https://tools.invalid",
        toolchain_workspace="tools-workspace",
        toolchain_access_key_id="tools-ak",
        toolchain_access_key_secret="tools-sk",
        toolchain_app_key="tools-app-key",
        toolchain_allowed_subjects=["demo-publisher"],
    )
    with TestClient(application(settings)) as client:
        yield client, state


def test_live_catalog_and_import_use_documented_contract_without_secret_fields(connected):
    client, state = connected
    defaults = client.get("/api/v1/tool-publication-defaults").json()
    assert defaults["toolchainMode"] == "live"
    assert defaults["toolchainUrl"] == "https://workbench.invalid/tools"
    assert defaults["mode"] == "simulation"  # 成果查询不代表真实执行已对接。
    listing = client.get("/api/v1/toolchain-tools")
    assert listing.status_code == 200 and listing.json()["mode"] == "live"
    assert "private-token" not in listing.text and "serviceConfig" not in listing.text
    item = listing.json()["items"][0]
    assert item["externalId"] == "external-tool-1" and item["inputSchema"] == ""
    path = "/api/v1/toolchain-tools/external-tool-1/import?version=" + item["version"]
    result = client.post(path)
    assert result.status_code == 200, result.text
    draft = result.json()
    assert draft["status"] == "draft" and draft["data"]["name"] == "工具成果"
    assert draft["source"]["workspace"] == "tools-workspace"
    assert draft["source"]["mode"] == "live"
    assert "private-token" not in result.text
    assert client.post(path).json()["id"] == draft["id"]
    assert any(call.url.path.endswith("/getOne") for call in state["calls"])


@pytest.mark.parametrize("failure", ["version", "workspace", "code", "detail-version", "network"])
def test_invalid_or_changed_live_result_does_not_create_draft(connected, failure):
    client, state = connected
    if failure == "workspace":
        state["detail"] = supplier_tool(workspaceCode="other-workspace")
    elif failure == "code":
        state["detail"] = supplier_tool(code="different-tool")
    elif failure == "detail-version":
        state["detail"] = supplier_tool(version="different-version")
    elif failure == "network":
        state["failure"] = True
    version = "old-version" if failure == "version" else "1767939302512"
    response = client.post("/api/v1/toolchain-tools/external-tool-1/import?version=" + version)
    expected = 502 if failure == "network" else 404 if failure == "workspace" else 409
    assert response.status_code == expected
    assert client.get("/api/v1/tool-publications").json() == []


def test_live_allowlist_denies_requests_before_accessing_supplier(connected):
    client, state = connected
    token, _ = client.app.state.application_identity.select("demo-org-admin")
    client.cookies.set("application_user", token)
    assert client.get("/api/v1/toolchain-tools").status_code == 403
    assert client.post("/api/v1/toolchain-tools/external-tool-1/import").status_code == 403
    assert state["calls"] == []


def test_foreign_workspace_is_not_in_catalog(connected):
    client, state = connected
    state["rows"].append(supplier_tool(code="other", workspaceCode="other-workspace"))
    assert len(client.get("/api/v1/toolchain-tools").json()["items"]) == 1
    assert client.post("/api/v1/toolchain-tools/other/import").status_code == 404


def test_catalog_reads_all_pages_and_configured_types(connected):
    client, _ = connected
    provider = client.app.state.tool_workspace.provider
    provider.client.settings = provider.client.settings.model_copy(
        update={"toolchain_tools_types": ["api", "MY_MCP"], "toolchain_tools_category": "platform"}
    )
    provider.client.request = Mock(
        side_effect=[
            {"total": 2, "list": [supplier_tool()]},
            {"total": 2, "list": [supplier_tool(code="external-tool-2")]},
            {"total": 1, "list": [supplier_tool(code="external-tool-3", toolType="MY_MCP")]},
        ]
    )
    result = client.get("/api/v1/toolchain-tools")
    assert result.status_code == 200
    assert len(result.json()["items"]) == 3
    bodies = [call.kwargs["body"] for call in provider.client.request.call_args_list]
    assert [(body["current"], body["toolType"]) for body in bodies] == [
        (1, "api"),
        (2, "api"),
        (1, "MY_MCP"),
    ]
    assert all(body["toolCategory"] == "platform" for body in bodies)


@pytest.mark.parametrize("case", ["total", "empty-page", "duplicate", "missing-version"])
def test_catalog_rejects_malformed_or_incomplete_pages(connected, case):
    client, _ = connected
    provider = client.app.state.tool_workspace.provider
    pages = [{"total": 2, "list": [supplier_tool()]}]
    if case == "total":
        pages[0]["total"] = True
    elif case == "empty-page":
        pages.append({"total": 2, "list": []})
    elif case == "duplicate":
        pages.append({"total": 2, "list": [supplier_tool()]})
    else:
        pages[0]["list"] = [supplier_tool(version=None)]
    provider.client.request = Mock(side_effect=pages)
    response = client.get("/api/v1/toolchain-tools")
    assert response.status_code == 502
    assert "items" not in response.json()


def test_sources_with_equal_codes_are_isolated_by_environment(connected):
    client, _ = connected
    workspace = client.app.state.tool_workspace
    candidate = workspace.provider.get_tool("external-tool-1")
    workspace.provider = Mock()
    workspace.provider.get_tool.return_value = candidate
    path = "/api/v1/toolchain-tools/external-tool-1/import"
    first = client.post(path).json()
    for field in ("workspace", "environment", "mode"):
        workspace.provider.get_tool.return_value = {
            **candidate,
            "source": {**candidate["source"], field: "different"},
        }
        next_draft = client.post(path).json()
        assert next_draft["id"] != first["id"]
        assert next_draft["toolId"] != first["toolId"]


def test_full_tool_configuration_roundtrip_and_legacy_switch(tmp_path):
    with TestClient(application()) as client:
        data = payload(client)
        data["configuration"]["tools"].update(
            entryUrl="https://tools-workbench.invalid/develop",
            toolCategory="platform",
            toolTypes=["MY_MCP"],
        )
        data["configuration"]["tools"]["connection"].update(
            mode="live",
            url="https://tools.invalid",
            workspace="tools-space",
            allowedSubjects=["demo-publisher"],
            timeout=12,
        )
        data["credentials"] = {
            "toolsAccessKeyId": "only-tools-ak",
            "toolsAccessKeySecret": "only-tools-sk",
        }
        saved = client.put(PATH, json=data)
        assert saved.status_code == 200, saved.text
        assert "only-tools-ak" not in saved.text and "only-tools-sk" not in saved.text
        assert saved.json()["readiness"]["tools"]["checks"][1]["status"] == "configured"
        assert saved.json()["readiness"]["tools"]["checks"][-1]["status"] == "supplier"
        assert client.get("/api/v1/toolchain-tools").status_code == 503
    with TestClient(application()) as restarted:
        settings = restarted.app.state.toolchain_clients["tools"].settings
        assert settings.toolchain_workspace == "tools-space"
        assert settings.toolchain_tools_category == "platform"
        assert settings.toolchain_tools_types == ["MY_MCP"]
        assert settings.toolchain_timeout == 12
        assert settings.toolchain_access_key_id.get_secret_value() == "only-tools-ak"
        assert restarted.app.state.toolchain_clients["applications"].mode == "disabled"
        path = restarted.app.state.toolchain_configuration.path
    document = json.loads(path.read_text("utf-8"))
    document["configuration"]["tools"] = {"mode": "mock"}
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    with TestClient(application()) as legacy:
        assert legacy.get(PATH).json()["configuration"]["tools"]["connection"]["mode"] == "mock"
        assert legacy.get("/api/v1/toolchain-tools").status_code == 200
    assert path.read_bytes() == before
