"""工具模拟闭环验证；所有数据库连接均禁止，防止开发流程误用真实存储。"""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import Settings
from app.main import create_app
from app.modules.common.identity import DEFAULT_USER
from app.modules.tools.providers import INPUT_SCHEMA, OUTPUT_SCHEMA


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(Engine, "connect", Mock(side_effect=AssertionError("no database access")))
    with TestClient(create_app(Settings(_env_file=None))) as value:
        yield value


def tool_data(**changes):
    return {
        "name": "测试工具",
        "version": "v1.0",
        "description": "工具简介",
        "descriptionMarkdown": "## 使用说明\n用于验证发布流程",
        "toolType": "文本处理",
        "tags": ["测试"],
        "sourceType": "自研工具",
        "environment": "CPU",
        "deployment": "API接入",
        "inputSchema": INPUT_SCHEMA,
        "outputSchema": OUTPUT_SCHEMA,
        "license": "内部使用",
        "dataLevel": "一般数据",
        "endpoint": "",
        **changes,
    }


def approved(client):
    record = client.post(
        "/api/v1/tool-publications", json={"data": tool_data(), "submit": True}
    ).json()
    return approve_record(client, record)


def approve_record(client, record, **changes):
    """按七维评分完成一份申请，支持附加当前轮次的报告。"""
    scores = {
        key: 4
        for key in (
            "formatCompliance",
            "safetyCompliance",
            "annotationCompliance",
            "structuralCompleteness",
            "authenticity",
            "consistency",
            "cleanliness",
        )
    }
    response = client.post(
        f"/api/v1/admin/tool-publications/{record['id']}/review",
        json={
            "revision": record["revision"],
            "decision": "approved",
            "scores": scores,
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_return_resubmit_and_publish_without_endpoint(client):
    response = client.post("/api/v1/tool-publications", json={"data": {"name": "未完整草稿"}})
    assert response.status_code == 201
    record = response.json()
    assert record["status"] == "draft"
    assert client.get(f"/api/v1/tools/{record['toolId']}").status_code == 404
    record = client.put(
        f"/api/v1/tool-publications/{record['id']}",
        json={
            "data": tool_data(),
            "submit": True,
            "revision": 1,
        },
    ).json()
    assert record["status"] == "pending"
    queue = client.get("/api/v1/admin/tool-publications?status=pending&pageSize=1").json()
    assert queue["total"] == 1 and queue["items"][0]["id"] == record["id"]
    review = f"/api/v1/admin/tool-publications/{record['id']}/review"
    assert client.post(review, json={"decision": "approved", "revision": 2}).status_code == 422
    returned = client.post(
        review, json={"decision": "needs_changes", "revision": 2, "reason": "补充说明"}
    ).json()
    assert returned["status"] == "needs_changes"
    assert returned["reviews"][0]["snapshot"]["description"] == "工具简介"
    assert (
        client.put(
            f"/api/v1/tool-publications/{record['id']}", json={"data": tool_data(), "revision": 2}
        ).status_code
        == 409
    )
    resubmitted = client.put(
        f"/api/v1/tool-publications/{record['id']}",
        json={"data": tool_data(), "submit": True, "revision": 3},
    ).json()
    assert resubmitted["status"] == "pending"
    assert len(resubmitted["reviews"]) == 1
    published = approved(client)
    detail = client.get(f"/api/v1/tools/{published['toolId']}").json()
    assert detail["name"] == "测试工具" and detail["inputSchema"] == INPUT_SCHEMA
    assert not {"dataLevel", "endpoint", "organizationId", "contact"}.intersection(detail)
    assert client.get(f"/api/v1/admin/tools/{published['toolId']}").json()["endpoint"] == ""
    assert (
        client.put(
            f"/api/v1/tool-publications/{published['id']}",
            json={"data": tool_data(), "revision": published["revision"]},
        ).status_code
        == 409
    )


def test_tools_catalog_facets_favorites_and_per_account_ratings(client):
    page = client.get("/api/v1/tools?pageSize=2").json()
    assert page["total"] == 12 and len(page["items"]) == 2
    item = page["items"][0]
    path = f"/api/v1/tools/{item['id']}"
    assert client.get("/api/v1/tools?favoritesOnly=true").json()["total"] == 0
    for _ in range(2):
        assert client.put(path + "/favorite", json={"favorite": True}).json()["favoriteCount"] == 1
    assert client.get("/api/v1/tools?favoritesOnly=true").json()["total"] == 1
    assert (
        client.get("/api/v1/tools", params={"facets": '{"team":["不存在"]}'}).json()["total"] == 0
    )
    for rating in (3, 5):
        result = client.put(path + "/rating", json={"rating": rating, "comment": "评价"}).json()
        assert result["rating"] == rating and result["ratingCount"] == 1
    assert client.put(path + "/rating", json={"rating": 6}).status_code == 422
    token, _ = client.app.state.application_identity.select("demo-reader")
    client.cookies.set("application_user", token)
    result = client.get(path).json()
    assert not result["isFavorite"] and result["myRating"] == 0 and result["ratingCount"] == 1


def test_unlisting_and_visibility_protect_details_interactions_and_legacy_route(client):
    tool_id = approved(client)["toolId"]
    path = f"/api/v1/tools/{tool_id}"
    admin = f"/api/v1/admin/tools/{tool_id}"
    scope = {"scope": "organizations", "targets": ["org-research"], "revision": 1}
    assert client.put(admin + "/visibility", json=scope).status_code == 200
    assert client.put(admin + "/visibility", json=scope).status_code == 409
    assert (
        client.put(
            admin + "/visibility", json={**scope, "revision": 2, "targets": ["fake"]}
        ).status_code
        == 422
    )
    token, _ = client.app.state.application_identity.select("demo-outsider")
    client.cookies.set("application_user", token)
    assert client.get(path).status_code == 404
    assert client.put(path + "/favorite", json={"favorite": True}).status_code == 404
    assert (
        client.post(f"/api/v1/resources/{tool_id}/invoke", json={"prompt": "绕过"}).status_code
        == 404
    )
    assert client.post(f"/api/v1/resources/{tool_id}/access-requests", json={}).status_code == 404
    assert (
        client.put(admin + "/listing", json={"status": "unlisted", "revision": 1}).status_code
        == 200
    )
    client.cookies.clear()
    assert client.get(path).status_code == 404
    assert client.get(admin).json()["listingStatus"] == "unlisted"
    assert client.get("/api/v1/admin/tool-audit").json()["total"] == 3


def test_invalid_schema_and_inputs_do_not_create_fake_success(client):
    assert (
        client.post(
            "/api/v1/tool-publications",
            json={"data": tool_data(inputSchema='{"type":"bad"}'), "submit": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/tool-publications",
            json={"data": tool_data(), "submit": True, "createdBy": "admin"},
        ).status_code
        == 422
    )
    item = client.get("/api/v1/tools").json()["items"][0]
    path = f"/api/v1/tools/{item['id']}"
    assert client.post(path + "/invoke", json={"inputs": {"wrong": 2}}).status_code == 422
    success = client.post(path + "/invoke", json={"inputs": {"text": "测试"}})
    assert success.status_code == 200 and success.json()["mode"] == "simulation"
    assert (
        client.post(
            path + "/invoke", json={"inputs": {"text": "测试"}, "simulateFailure": True}
        ).status_code
        == 502
    )
    stats = client.get(f"/api/v1/admin/tools/{item['id']}").json()["runtime"]
    assert stats["total"] == 2 and stats["successRate"] == 50 and stats["errorRate"] == 50


def test_toolchain_import_is_atomic_idempotent_and_isolated(client):
    candidate = client.get("/api/v1/toolchain-tools").json()["items"][0]
    workspace = client.app.state.tool_workspace
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(
            pool.map(
                lambda _: workspace.import_tool(
                    candidate["externalId"],
                    DEFAULT_USER,
                    {"owner": "研发单位", "contact": "研发发布者"},
                ),
                range(8),
            )
        )
    assert len({item["id"] for item in records}) == 1
    assert records[0]["status"] == "draft"
    assert client.get("/api/v1/tools").json()["total"] == 12
    assert client.get("/api/v1/tool-publications").json()[0]["data"]["inputSchema"] == INPUT_SCHEMA
    with TestClient(create_app(Settings(_env_file=None))) as other:
        assert other.get("/api/v1/tool-publications").json() == []


def test_replaced_provider_propagates_mode_and_records_output_failures(client):
    """替换供应方后模式随接口返回，非法输出与服务异常都应计入失败。"""
    provider = Mock(mode="live")
    provider.invoke.side_effect = [
        {"wrong": 1},
        RuntimeError("供应方内部异常"),
        {"result": "已处理"},
    ]
    client.app.state.tool_workspace.provider = provider
    assert client.get("/api/v1/tool-publication-defaults").json()["mode"] == "live"
    page = client.get("/api/v1/tools").json()
    tool_id = page["items"][0]["id"]
    assert page["mode"] == "live" and page["items"][0]["runtimeMode"] == "live"
    path = f"/api/v1/tools/{tool_id}/invoke"
    assert (
        client.post(path, json={"inputs": {"text": "测试"}, "simulateFailure": True}).status_code
        == 422
    )
    provider.invoke.assert_not_called()
    for message in ("JSON Schema", "工具服务未能完成调用"):
        response = client.post(path, json={"inputs": {"text": "测试"}})
        assert response.status_code == 502 and message in response.json()["detail"]
    success = client.post(path, json={"inputs": {"text": "测试"}})
    assert success.status_code == 200 and success.json()["mode"] == "live"
    assert success.json()["output"] == {"result": "已处理"}
    stats = client.get(f"/api/v1/admin/tools/{tool_id}").json()["runtime"]
    assert stats["total"] == 3 and stats["errorRate"] == 66.67


def test_admin_permissions_and_readonly_field_rules(client):
    config = client.get("/api/v1/admin/tool-configuration").json()
    rules = {item["key"]: item["requirement"] for item in config["fields"]}
    assert sum(value == "必填" for value in rules.values()) == 12
    assert rules["version"] == "系统生成"
    for key in ("team", "technicalStack", "endpoint", "usageExample"):
        assert rules[key] == "选填"
    assert client.put("/api/v1/admin/tool-configuration", json=config).status_code == 405
    token, _ = client.app.state.application_identity.select("demo-reader")
    client.cookies.set("application_admin", token)
    assert client.get("/api/v1/admin/tools").status_code == 403
    assert client.get("/api/v1/admin/tool-publications").status_code == 403
    assert client.get("/api/v1/admin/tool-configuration").status_code == 403
    client.cookies.set("application_user", token)
    assert client.post("/api/v1/tool-publications", json={"data": tool_data()}).status_code == 403


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "description",
        "descriptionMarkdown",
        "toolType",
        "tags",
        "sourceType",
        "environment",
        "deployment",
        "inputSchema",
        "outputSchema",
        "license",
        "dataLevel",
    ],
)
def test_task_required_fields_enforced_only_on_submission(client, field):
    """任务表 59 行的十二项必填在 API 生效，未完成资料仍能保存草稿。"""
    data = tool_data()
    data.pop(field)
    assert client.post("/api/v1/tool-publications", json={"data": data}).status_code == 201
    response = client.post("/api/v1/tool-publications", json={"data": data, "submit": True})
    assert response.status_code == 422


def test_system_version_sensitive_precheck_and_optional_examples(client):
    """不额外要求版本或示例；选填内容存在时必须符合声明，最终提交不能绕过预检。"""
    data = tool_data()
    data.pop("version")
    response = client.post("/api/v1/tool-publications", json={"data": data, "submit": True})
    assert response.status_code == 201 and response.json()["data"]["version"] == "v1.0"
    response = client.post(
        "/api/v1/tool-publications", json={"data": tool_data(version="伪造版本")}
    )
    assert response.json()["data"]["version"] == "v1.0"
    blocked = tool_data(description="包含演示拦截词")
    precheck = client.post("/api/v1/tool-publication-precheck", json=blocked)
    assert "description" in precheck.json()["fieldErrors"]
    assert client.post("/api/v1/tool-publications", json={"data": blocked}).status_code == 201
    assert (
        client.post("/api/v1/tool-publications", json={"data": blocked, "submit": True}).status_code
        == 422
    )
    for example in ("非 JSON", '{"wrong": 1}'):
        assert (
            client.post(
                "/api/v1/tool-publications",
                json={"data": tool_data(inputExample=example), "submit": True},
            ).status_code
            == 422
        )


def test_version_review_preserves_live_resource_visibility_and_statistics(client):
    """版本审核前目录保持旧内容；通过后沿用编号、历史、互动、权限和下架决定。"""
    first = approved(client)
    tool_id = first["toolId"]
    path = f"/api/v1/tools/{tool_id}"
    admin = f"/api/v1/admin/tools/{tool_id}"
    original = client.get(admin).json()
    client.put(path + "/favorite", json={"favorite": True})
    client.post(path + "/invoke", json={"inputs": {"text": "调用原版本"}})
    visibility = {"scope": "projects", "targets": ["project-a"], "revision": 1}
    assert client.put(admin + "/visibility", json=visibility).status_code == 200
    assert (
        client.put(
            admin + "/visibility",
            json={"scope": "members", "targets": ["demo-reader"], "revision": 2},
        ).status_code
        == 422
    )
    draft = client.post(path + "/versions").json()
    assert draft["data"]["version"] == "v2.0"
    assert client.post(path + "/versions").json()["id"] == draft["id"]
    token, _ = client.app.state.application_identity.select("demo-org-admin")
    client.cookies.set("application_user", token)
    assert client.post(path + "/versions").status_code == 403
    client.cookies.clear()
    pending = client.put(
        f"/api/v1/tool-publications/{draft['id']}",
        json={
            "data": {**draft["data"], "description": "第二版简介"},
            "submit": True,
            "revision": 1,
        },
    ).json()
    assert client.get(path).json()["description"] == original["description"]
    assert (
        client.put(admin + "/listing", json={"status": "unlisted", "revision": 1}).status_code
        == 200
    )
    approve_record(client, pending)
    latest = client.get(admin).json()
    assert latest["version"] == "v2.0" and latest["description"] == "第二版简介"
    assert latest["id"] == original["id"] and latest["createdAt"] == original["createdAt"]
    assert latest["favoriteCount"] == 1 and latest["usage"] == 1
    assert latest["visibility"]["scope"] == "projects"
    assert latest["listingStatus"] == "unlisted" and latest["revision"] == 3
    assert [item["version"] for item in latest["versions"]] == ["v2.0", "v1.0"]
    assert latest["versions"][0]["changedFields"] == ["version", "description"]
    assert client.get(path + "/definition").status_code == 404


def test_toolchain_new_version_updates_existing_tool_without_overwriting_draft(client):
    """工具链同版本导入幂等，新版本复用工具编号并经过审核。"""
    chain = client.get("/api/v1/toolchain-tools").json()
    assert chain["entryUrl"] == "/tools/toolchain/workbench"
    candidate = chain["items"][0]
    path = f"/api/v1/toolchain-tools/{candidate['externalId']}/import"
    draft = client.post(path).json()
    pending = client.put(
        f"/api/v1/tool-publications/{draft['id']}",
        json={"data": draft["data"], "submit": True, "revision": draft["revision"]},
    ).json()
    first = approve_record(client, pending)
    provider = Mock(mode="simulation")
    provider.get_tool.return_value = {**candidate, "version": "v9.0"}
    client.app.state.tool_workspace.provider = provider
    second = client.post(path).json()
    assert second["toolId"] == first["toolId"] and second["data"]["version"] == "v9.0"
    assert client.post(path).json()["id"] == second["id"]
    provider.get_tool.return_value = {**candidate, "version": "v10.0"}
    assert client.post(path).status_code == 409
    pending = client.put(
        f"/api/v1/tool-publications/{second['id']}",
        json={"data": second["data"], "submit": True, "revision": second["revision"]},
    ).json()
    approve_record(client, pending)
    detail = client.get(f"/api/v1/admin/tools/{first['toolId']}").json()
    assert detail["version"] == "v9.0" and len(detail["versions"]) == 2
    assert detail["source"]["externalId"] == candidate["externalId"]


def test_reports_are_scoped_to_reviewer_publication_revision_and_immutable_history(client):
    """报告复用既有校验且全程内存存储，不接受跨人、跨申请或过期附件。"""
    record = client.post(
        "/api/v1/tool-publications", json={"data": tool_data(), "submit": True}
    ).json()
    base = f"/api/v1/admin/tool-publications/{record['id']}"
    response = client.post(
        base + "/report-files?revision=1",
        files={
            "file": (
                "质量报告.md",
                "# 质量评测\n检查通过<script>bad()</script>".encode(),
                "text/markdown",
            )
        },
    )
    assert response.status_code == 201, response.text
    report = response.json()
    content_path = base + f"/report-files/{report['id']}/content"
    assert client.get(base + "/report-files").json() == [report]
    assert "content" not in report and "preview" not in report
    preview = client.get(content_path + "?preview=true")
    assert preview.status_code == 200 and "<script>" not in preview.text
    assert "sandbox" in preview.headers["content-security-policy"]
    assert client.head(content_path).headers["content-length"] == str(report["size"])
    assert (
        client.post(
            base + "/report-files?revision=2", files={"file": ("stale.txt", b"report")}
        ).status_code
        == 409
    )
    token, _ = client.app.state.application_identity.select("demo-org-admin")
    client.cookies.set("application_admin", token)
    assert client.get(content_path).status_code == 404
    assert (
        client.post(
            base + "/review",
            json={
                "revision": 1,
                "decision": "needs_changes",
                "reason": "补充",
                "reportFileIds": [report["id"]],
            },
        ).status_code
        == 422
    )
    client.cookies.clear()
    other = client.post(
        "/api/v1/tool-publications", json={"data": tool_data(), "submit": True}
    ).json()
    assert (
        client.post(
            f"/api/v1/admin/tool-publications/{other['id']}/review",
            json={
                "revision": 1,
                "decision": "needs_changes",
                "reason": "补充",
                "reportFileIds": [report["id"]],
            },
        ).status_code
        == 422
    )
    reviewed = approve_record(client, record, reportFileIds=[report["id"]])
    assert reviewed["reviews"][0]["reportFiles"] == [report]
    assert client.delete(base + f"/report-files/{report['id']}").status_code == 409
    assert client.get(base + "/report-files").json() == []
    client.cookies.set("application_admin", token)
    assert client.get(content_path).status_code == 200
    reader, _ = client.app.state.application_identity.select("demo-reader")
    client.cookies.set("application_admin", reader)
    assert client.get(content_path).status_code == 403


def test_priority_submission_dates_and_reviewer_audit_filters(client):
    records = [
        client.post(
            "/api/v1/tool-publications",
            json={
                "data": tool_data(name=f"队列 {index}"),
                "submit": True,
            },
        ).json()
        for index in range(2)
    ]
    first = records[0]
    path = f"/api/v1/admin/tool-publications/{first['id']}/priority"
    assert client.put(path, json={"revision": 1, "priority": "urgent"}).status_code == 200
    queue = client.get("/api/v1/admin/tool-publications?status=pending&sort=priority").json()
    assert queue["items"][0]["id"] == first["id"]
    today = first["submittedAt"][:10]
    assert (
        client.get(f"/api/v1/admin/tool-publications?start={today}&end={today}").json()["total"]
        == 2
    )
    assert client.get("/api/v1/admin/tool-publications?end=2000-01-01").json()["total"] == 0
    assert (
        client.get("/api/v1/admin/tool-publications?start=2026-02-01&end=2026-01-01").status_code
        == 422
    )
    approve_record(client, first)
    assert client.put(path, json={"revision": 2, "priority": "normal"}).status_code == 409
    audit = client.get(
        "/api/v1/admin/tool-audit",
        params={
            "decision": "approved",
            "reviewer": "系统管理员",
            "start": today,
            "end": today,
        },
    ).json()
    assert audit["total"] == 1 and audit["items"][0]["publicationId"] == first["id"]
    assert client.get("/api/v1/admin/tool-audit?reviewer=unknown").json()["total"] == 0


def test_tool_definition_supports_complete_schema_and_permission_checked_invocation(client):
    record = client.post(
        "/api/v1/tool-publications",
        json={
            "data": tool_data(
                inputSchema='{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"array","items":{"type":"integer"},"minItems":1}',
                outputSchema='{"type":"integer"}',
                outputExample="3",
            ),
            "submit": True,
        },
    ).json()
    published = approve_record(client, record)
    path = f"/api/v1/tools/{published['toolId']}"
    definition = client.get(path + "/definition").json()
    assert definition["inputSchema"]["type"] == "array" and definition["mode"] == "simulation"
    assert "endpoint" not in definition
    invocation = definition["invocation"]
    result = client.request(
        invocation["method"], invocation["path"], json={invocation["inputField"]: [1, 2]}
    )
    assert result.status_code == 200 and result.json()["output"] == 3
    assert client.post(path + "/invoke", json={"inputs": ["wrong"]}).status_code == 422
    assert (
        client.put(
            f"/api/v1/admin/tools/{published['toolId']}/listing",
            json={
                "status": "unlisted",
                "revision": 1,
            },
        ).status_code
        == 200
    )
    assert client.get(path + "/definition").status_code == 404
    assert client.post(path + "/invoke", json={"inputs": [1]}).status_code == 404
