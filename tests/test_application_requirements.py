"""任务书差距回归：实时校验、能力推荐、真实运行合同、可信权限映射。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import Settings
from app.main import create_app
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.discovery import analyze, related
from app.modules.applications.identity import DEFAULT_USER, SessionIdentityProvider, can_manage
from app.modules.applications.identity_directory import publication_defaults, visible_directory
from app.modules.applications.memory_repository import InMemoryApplicationRepository
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.runtime import AgentDevProvider
from app.modules.applications.workflow_schemas import RunInput


def test_precheck_reports_field_errors_without_saving_and_submit_still_rejects():
    with TestClient(create_app(Settings())) as client:
        before = client.get("/api/v1/application-publications").json()
        response = client.post(
            "/api/v1/application-precheck",
            json={
                "data": {
                    "name": "演示拦截词",
                    "owner": " ",
                    "inputSchema": "{",
                    "tutorials": [{"title": "教程", "url": "javascript:alert(1)"}],
                }
            },
        )
        assert response.status_code == 200
        errors = response.json()["fieldErrors"]
        assert {"name", "owner", "inputSchema", "tutorials", "summary"} <= errors.keys()
        assert "演示拦截词" not in response.text  # 不返回命中的敏感词原文或整份词表。
        assert client.get("/api/v1/application-publications").json() == before
        data = {"name": "演示拦截词", "owner": "单位", "summary": "简介"}
        assert (
            client.post(
                "/api/v1/application-publications", json={"data": data, "submit": True}
            ).status_code
            == 422
        )


def test_precheck_accepts_incomplete_fields_and_category_does_not_guess_on_ties():
    with TestClient(create_app(Settings())) as client:
        response = client.post(
            "/api/v1/application-precheck",
            json={
                "data": {
                    "capabilities": [None],
                    "dependencies": [{"kind": [], "resourceId": "x"}],
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["fieldErrors"]
        checked = client.post(
            "/api/v1/application-precheck",
            json={
                "data": {
                    "name": "多语言翻译助手",
                    "capabilities": ["技术资料翻译"],
                }
            },
        ).json()
        assert checked["discovery"]["suggestedCategory"] == "翻译沟通"
        assert checked["discovery"]["reasons"]
        assert analyze({"name": "未知功能"})["suggestedCategory"] is None
        assert analyze({"name": "翻译与合同"})["suggestedCategory"] is None


def test_automatic_category_is_applied_on_save_and_manual_choice_is_preserved():
    data = {"name": "合同条款审查", "category": "文档创作"}
    with TestClient(create_app(Settings())) as client:
        automatic = client.post(
            "/api/v1/application-publications", json={"data": data, "autoClassify": True}
        )
        manual = client.post("/api/v1/application-publications", json={"data": data})
        assert automatic.json()["data"]["category"] == "法律合规"
        assert manual.json()["data"]["category"] == "文档创作"


def test_recommendations_match_capabilities_and_do_not_leak_hidden_resources():
    source = {"id": "a", "name": "应用A", "capabilities": ["专业术语翻译"], "category": "翻译沟通"}
    candidates = [
        {"id": "b", "name": "应用B", "capabilities": ["专业术语翻译"], "category": "翻译沟通"},
        {"id": "c", "name": "合同检查", "category": "法律合规"},
    ]
    assert [item["id"] for item in related(source, candidates)] == ["b"]
    with TestClient(create_app(Settings())) as client:
        service = client.app.state.application_service
        with service.repository.transaction() as session:
            for item in session.list_resources():
                if item["id"] != "app-qa":
                    session.put_visibility(
                        item["id"], {"scope": "members", "targets": ["outsider"], "revision": 2}
                    )
        result = client.get("/api/v1/applications/app-qa/discovery")
        assert result.status_code == 200
        assert result.json()["related"] == []
        assert client.get("/api/v1/applications/app-ppt/discovery").status_code == 404


def test_recommendation_does_not_treat_ubiquitous_labels_as_business_similarity():
    source = {
        "id": "source",
        "name": "翻译助手",
        "category": "翻译沟通",
        "capabilities": ["多轮交互", "术语翻译"],
    }
    candidates = [
        {
            "id": str(index),
            "name": f"应用{index}",
            "category": "数据处理",
            "capabilities": ["多轮交互"],
        }
        for index in range(5)
    ]
    candidates[0]["capabilities"].append("术语翻译")
    assert [item["id"] for item in related(source, candidates)] == ["0"]


def live_service(tmp_path, handler):
    repository = InMemoryApplicationRepository()
    with repository.transaction() as session:
        resource = session.get_resource("app-qa")
    bindings = tmp_path / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "app-qa": {
                    "version_id": resource["versionId"],
                    "agent_code": "agent-123",
                    "agent_version": "version-456",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        applications_runtime="agent_dev",
        applications_agent_url="https://agent.example.test",
        applications_agent_key="test-secret",
        applications_agent_bindings=str(bindings),
    )
    provider = AgentDevProvider(DemoApplicationProvider(), settings, httpx.MockTransport(handler))
    return ApplicationService(repository, provider)


def test_documented_live_protocol_success_and_duplicate_request_count_once(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer test-secret"
        data = json.loads(request.content)
        if request.url.path.endswith("createSession"):
            assert data == {"agentCode": "agent-123", "agentVersion": "version-456"}
            return httpx.Response(
                200, json={"success": True, "data": {"uniqueCode": "session-real"}}
            )
        assert data["sessionId"] == "session-real" and data["stream"] is False
        assert data["message"]["text"] == "查询资料"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": {"value": "实际协议结果"}}],
                    }
                },
            },
        )

    service = live_service(tmp_path, handler)
    before = service.resource("app-qa")["usage"]
    payload = RunInput(request_id="one-request", input={"question": "查询资料"})
    result = service.workflows.run("app-qa", DEFAULT_USER, payload)
    assert result["status"] == "succeeded" and result["mode"] == "live"
    assert result["output"]["text"] == "实际协议结果"
    assert service.workflows.run("app-qa", DEFAULT_USER, payload) == result
    assert len(calls) == 2
    assert service.resource("app-qa")["usage"] == before + 1
    assert service.workflows.runtime("app-qa")["status"] == "live"
    assert service.workflows.runtime("app-qa")["sampleCount"] == 1


@pytest.mark.parametrize("failure", ["business", "timeout", "redirect", "empty"])
def test_failed_live_calls_never_fall_back_to_simulation_or_increment_usage(tmp_path, failure):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"success": True, "data": {"uniqueCode": "session"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("secret upstream details", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example.test"})
        return httpx.Response(200, json={"success": failure == "empty", "data": {}})

    service = live_service(tmp_path, handler)
    before = service.resource("app-qa")["usage"]
    result = service.workflows.run(
        "app-qa", DEFAULT_USER, RunInput(request_id="failure-request", input={"question": "查询"})
    )
    assert result["status"] == "failed" and result["mode"] == "live"
    assert result["output"] is None
    assert "secret" not in result["error"]
    assert service.resource("app-qa")["usage"] == before
    assert len(calls) == 2


def test_missing_live_configuration_blocks_run_and_factory_uses_live_provider():
    with TestClient(create_app(Settings(applications_runtime="agent_dev"))) as client:
        resource = client.get("/api/v1/applications/app-qa").json()
        assert resource["runtimeConnection"]["configured"] is False
        response = client.post(
            "/api/v1/applications/app-qa/runs",
            json={"requestId": "no-config", "input": {"question": "查询"}},
        )
        assert response.status_code == 503
        assert client.get("/api/v1/application-activity").json()["runs"] == []


def directory_request(path, subject="person-a", audience="admin"):
    request = Request(
        {
            "type": "http",
            "headers": [(b"x-role", b"admin")],
            "app": SimpleNamespace(
                state=SimpleNamespace(settings=Settings(applications_identity_directory=str(path)))
            ),
        }
    )
    request.state.business_identity = {"id": subject, "name": "登录名", "scope": audience}
    return request


def test_trusted_directory_maps_roles_and_scopes_and_revocation_takes_effect(tmp_path):
    path = tmp_path / "identity.json"
    directory = {
        "organizations": [{"id": "org-a", "name": "单位A"}, {"id": "org-b", "name": "单位B"}],
        "projects": [{"id": "project-a", "name": "项目A", "organizationId": "org-a"}],
        "members": [
            {
                "id": "person-a",
                "name": "单位管理员",
                "role": "org_admin",
                "organizationId": "org-a",
                "projectIds": ["project-a"],
            },
            {"id": "person-b", "name": "普通人员", "role": "user", "organizationId": "org-b"},
        ],
    }
    path.write_text(json.dumps(directory), encoding="utf-8")
    provider = SessionIdentityProvider()
    request = directory_request(path)
    actor = provider.resolve(request, "admin")
    assert actor.organization_id == "org-a" and actor.project_ids == ("project-a",)
    assert publication_defaults(request, actor) == {
        "owner": "单位A",
        "contact": "单位管理员",
        "organizationId": "org-a",
        "ownerAvailable": True,
    }
    assert can_manage(actor, {"organizationId": "org-a"})
    assert not can_manage(actor, {"organizationId": "org-b"})
    assert [item["id"] for item in visible_directory(request, actor)["members"]] == ["person-a"]
    with pytest.raises(HTTPException) as error:
        provider.resolve(directory_request(path, "person-b"), "admin")
    assert error.value.status_code == 403  # 浏览器请求头不能提升角色。
    directory["members"][0]["active"] = False
    path.write_text(json.dumps(directory), encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        provider.resolve(directory_request(path), "admin")
    assert error.value.status_code == 403
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        provider.resolve(directory_request(path), "admin")
    assert error.value.status_code == 503
