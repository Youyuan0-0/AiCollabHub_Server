import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from minio import Minio
from redis import Redis
from sqlalchemy import Engine

from app.core.config import Settings
from app.main import create_app

BASE = "/api/v1/application-publications"
ADMIN = "/api/v1/admin/application-publications"


@pytest.fixture
def client(monkeypatch):
    connect = Mock(side_effect=AssertionError("Application demo must not access infrastructure"))
    monkeypatch.setattr(Engine, "connect", connect)
    monkeypatch.setattr(Redis, "execute_command", connect)
    monkeypatch.setattr(Minio, "_execute", connect)
    with TestClient(create_app(Settings())) as client:
        # 应用依赖读取本地知识库仓储，测试先完成真实创建审批，不再引用不存在的固定样例。
        base = client.post(
            "/api/v1/knowledge-bases",
            json={"name": "应用测试知识库", "description": "应用依赖回归资料"},
        ).json()
        path = f"/api/v1/admin/knowledge-bases/{base['id']}"
        approved = client.post(
            path + "/review", json={"revision": base["revision"], "decision": "approved"}
        )
        assert approved.status_code == 200, approved.text
        shared = client.put(
            path + "/visibility",
            json={"scope": "all", "targets": [], "revision": approved.json()["revision"]},
        )
        assert shared.status_code == 200, shared.text
        client.app.state.test_knowledge_id = base["id"]
        yield client
    connect.assert_not_called()


@pytest.fixture
def details(client):
    return {
        "name": "试验报告助手",
        "englishName": "Test Report Assistant",
        "version": "v2.3",
        "summary": "整理试验数据并辅助报告写作",
        "descriptionMarkdown": "## 能力\n\n- 报告写作",
        "category": "文档创作",
        "owner": "试验中心",
        "contact": "测试联系人",
        "developmentTeam": "应用研发组",
        "sharingAgreement": "限集团内共享，保留作者信息",
        "toolList": ["报告生成", "数据核验"],
        "tags": ["报告", "写作"],
        "capabilities": ["结构化输出"],
        "baseModel": "示例模型",
        "tutorials": [{"title": "操作指南", "kind": "document", "url": "/guides/test.html"}],
        "configOptions": [{"name": "输出格式", "value": "Markdown", "description": "文档格式"}],
        "usageLimits": "需要人工核对",
        "technicalStack": "Python",
        "deployment": "工具链托管",
        "triggerType": "用户手动触发",
        "communicationProtocol": "HTTP",
        "inputSchema": '{"type":"object","properties":{"question":{"type":"string"}}}',
        "outputSchema": '{"type":"object","properties":{"answer":{"type":"string"}}}',
        "dependencies": [
            {"kind": "model", "resourceId": "model-longcat"},
            {"kind": "dataset", "resourceId": "ds-testbench"},
            {"kind": "knowledge", "resourceId": client.app.state.test_knowledge_id},
            {"kind": "tool", "resourceId": "tool-ocr"},
        ],
        "versionNotes": "首版报告整理",
    }


def test_draft_roundtrip_preserves_fields_and_identifiers(client, details):
    response = client.post(BASE, json={"data": details})
    assert response.status_code == 201
    draft = response.json()
    assert draft["data"] == details
    assert draft["status"] == "draft"
    assert client.get(f"{BASE}/{draft['id']}").json() == draft
    updated = {**details, "name": "更新后的名称"}
    saved = client.put(
        f"{BASE}/{draft['id']}",
        json={
            "data": updated,
            "revision": draft["revision"],
        },
    ).json()
    for key in ("id", "applicationId", "versionId", "createdAt"):
        assert saved[key] == draft[key]
    assert saved["revision"] == draft["revision"] + 1
    assert len(client.get(BASE).json()) == 1
    assert client.get("/api/v1/applications", params={"q": updated["name"]}).json()["total"] == 0
    assert client.get(f"/api/v1/applications/{draft['applicationId']}").status_code == 404


def test_submit_review_and_catalog_share_one_application(client, details):
    record = client.post(BASE, json={"data": details, "submit": True}).json()
    assert record["status"] == "pending" and record["submittedAt"]
    assert client.get(ADMIN).json() == [record]
    assert (
        client.put(
            f"{BASE}/{record['id']}",
            json={
                "data": details,
                "revision": record["revision"],
            },
        ).status_code
        == 409
    )
    assert client.get("/api/v1/applications", params={"q": details["name"]}).json()["total"] == 0
    decision = {
        "decision": "approved",
        "scores": {
            "formatCompliance": 4,
            "safetyCompliance": 4,
            "annotationCompliance": 4,
            "structuralCompleteness": 4,
            "authenticity": 4,
            "consistency": 4,
            "cleanliness": 4,
        },
        "reason": "资料齐全",
        "revision": record["revision"],
    }
    approved = client.post(f"{ADMIN}/{record['id']}/review", json=decision)
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert client.post(f"{ADMIN}/{record['id']}/review", json=decision).status_code == 409
    page = client.get("/api/v1/applications", params={"q": details["name"]}).json()
    assert page["total"] == 1
    application = client.get(f"/api/v1/applications/{record['applicationId']}").json()
    assert application["versionId"] == record["versionId"]
    assert application["createdAt"] == record["createdAt"]
    assert application["englishName"] == details["englishName"]
    assert application["configOptions"] == details["configOptions"]
    assert application["rating"] is None and application["usage"] == 0
    assert application["downloadCount"] == 0 and application["favoriteCount"] == 0
    assert application["lastDownloadedAt"] is None
    assert application["serviceStatus"] == "not_connected"
    filtered = client.get(
        "/api/v1/applications",
        params={
            "q": details["name"],
            "facets": json.dumps({"runtimeState": ["未接入"]}),
        },
    ).json()
    assert filtered["total"] == 1
    assert client.get("/api/v1/applications?sort=rating").status_code == 200


def test_rejection_requires_reason_and_preserves_reviewed_snapshot(client, details):
    record = client.post(BASE, json={"data": details, "submit": True}).json()
    path = f"{ADMIN}/{record['id']}/review"
    assert client.post(path, json={"decision": "rejected", "revision": 1}).status_code == 422
    rejected = client.post(
        path,
        json={
            "decision": "rejected",
            "reason": "补充使用限制",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
            "revision": 1,
        },
    ).json()
    details["usageLimits"] = "补充后的限制"
    resubmitted = client.put(
        f"{BASE}/{record['id']}",
        json={
            "data": details,
            "submit": True,
            "revision": rejected["revision"],
        },
    ).json()
    assert resubmitted["status"] == "pending"
    assert resubmitted["applicationId"] == record["applicationId"]
    assert resubmitted["reviews"][0]["data"]["usageLimits"] == "需要人工核对"
    assert resubmitted["data"]["usageLimits"] == "补充后的限制"


def test_stale_edit_does_not_overwrite_latest_draft(client, details):
    record = client.post(BASE, json={"data": details}).json()
    update = {"data": {**details, "name": "新名称"}, "revision": record["revision"]}
    path = f"{BASE}/{record['id']}"
    assert client.put(path, json=update).status_code == 200
    assert client.put(path, json={"data": details, "revision": 1}).status_code == 409
    assert client.get(path).json()["data"]["name"] == "新名称"


def test_empty_drafts_are_allowed_but_submit_validates_and_rejects_unknown_fields(client):
    assert client.post(BASE, json={"data": {}}).status_code == 201
    assert client.post(BASE, json={"data": {}, "submit": True}).status_code == 422
    assert client.post(BASE, json={"data": {"visibility": "全集团"}}).status_code == 422
    assert len(client.get(BASE).json()) == 1
    assert (
        client.post(
            "/api/v1/publications",
            json={
                "kind": "application",
                "name": "旧表单",
                "summary": "不应静默丢失字段",
                "submit": True,
            },
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "//example.com", "/\\example.com", "data:text/html,test"]
)
def test_invalid_tutorial_url_does_not_create_record(client, details, url):
    details["tutorials"][0]["url"] = url
    assert client.post(BASE, json={"data": details}).status_code == 422
    assert client.get(BASE).json() == []


def test_visibility_is_admin_configuration_and_not_a_publication_field(client):
    application = client.get("/api/v1/applications").json()["items"][0]
    path = f"/api/v1/admin/applications/{application['id']}"
    assert client.put(path + "/visibility", json={"scope": "organizations"}).status_code == 422
    visibility = {"scope": "organizations", "targets": ["demo-org-1"]}
    assert client.put(path + "/visibility", json=visibility).json() == {**visibility, "revision": 2}
    assert client.get(path).json()["visibility"] == {**visibility, "revision": 2}
    assert "visibility" not in client.get(f"/api/v1/applications/{application['id']}").json()


def test_admin_queue_filters_before_pagination_and_retains_legacy_contract(client, details):
    pending = client.post(BASE, json={"data": details, "submit": True}).json()
    client.post(BASE, json={"data": {**details, "name": "另一应用", "category": "会议办公"}})
    client.post(BASE, json={"data": details})
    queue = client.get(
        "/api/v1/admin/application-review-queue",
        params={
            "q": "报告",
            "category": "文档创作",
            "status": "",
            "pageSize": 1,
        },
    ).json()
    assert queue["total"] == 2 and len(queue["items"]) == 1
    assert queue["counts"]["pending"] == 1 and queue["counts"]["draft"] == 1
    assert client.get("/api/v1/admin/application-review-queue").json()["items"] == [pending]
    assert len(client.get(ADMIN).json()) == 3
    assert client.get("/api/v1/admin/application-review-queue?status=unknown").status_code == 422


def configuration_payload(configuration, **overrides):
    return {
        **{
            key: configuration[key]
            for key in (
                "revision",
                "requiredFields",
                "fieldHints",
                "managementColumns",
            )
        },
        **overrides,
    }


def test_admin_configuration_changes_form_validation_without_breaking_pending(client, details):
    path = "/api/v1/admin/application-configuration"
    details["contact"] = ""
    pending = client.post(BASE, json={"data": details, "submit": True}).json()
    old = client.get(path).json()
    payload = configuration_payload(
        old,
        requiredFields=[*old["requiredFields"], "contact"],
        fieldHints={"contact": "填写负责此应用的联系人"},
        managementColumns=["downloadCount", "owner"],
    )
    saved = client.put(path, json=payload).json()
    assert saved["revision"] == old["revision"] + 1
    public = client.get("/api/v1/application-form-configuration").json()
    assert "managementColumns" not in public and "availableColumns" not in public
    contact = next(field for field in public["fields"] if field["key"] == "contact")
    assert contact["required"] and contact["helpText"] == payload["fieldHints"]["contact"]
    draft = client.post(BASE, json={"data": details}).json()
    created = client.post(BASE, json={"data": details, "submit": True})
    assert created.status_code == 201
    assert created.json()["data"]["contact"] == "研发发布者"
    # 新建时带出登录资料；编辑时明确清空必填联系人仍会被拦截。
    assert (
        client.put(
            f"{BASE}/{draft['id']}",
            json={"data": details, "submit": True, "revision": draft["revision"]},
        ).status_code
        == 422
    )
    approved = client.post(
        f"{ADMIN}/{pending['id']}/review",
        json={
            "decision": "approved",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
            "revision": pending["revision"],
        },
    )
    assert approved.status_code == 200
    assert approved.json()["formRevision"] == old["revision"]
    assert client.put(path, json=payload).status_code == 409
    assert client.get(path).json() == saved


@pytest.mark.parametrize(
    "overrides",
    [
        {"requiredFields": []},
        {"managementColumns": []},
        {"managementColumns": ["password"]},
        {"managementColumns": ["owner", "owner"]},
        {"fieldHints": {"password": "secret"}},
        {"fieldHints": {"contact": "x" * 301}},
    ],
)
def test_configuration_rejects_invalid_changes_atomically(client, overrides):
    path = "/api/v1/admin/application-configuration"
    original = client.get(path).json()
    assert client.put(path, json=configuration_payload(original, **overrides)).status_code == 422
    assert client.get(path).json() == original


def test_visibility_conflict_does_not_overwrite_latest_configuration(client):
    path = "/api/v1/admin/applications/app-qa"
    original = client.get(path).json()
    assert original["runtime"]["averageResponseMs"] is None
    assert original["runtime"]["successRate"] is None
    first = {"scope": "organizations", "targets": ["org-a"], "revision": 1}
    assert client.put(path + "/visibility", json=first).status_code == 200
    stale = {**first, "targets": ["org-b"]}
    assert client.put(path + "/visibility", json=stale).status_code == 409
    assert client.get(path).json()["visibility"]["targets"] == ["org-a"]


def test_demo_records_are_isolated_between_application_instances(client, details):
    original = client.get("/api/v1/applications").json()
    client.post(BASE, json={"data": details})
    with TestClient(create_app(Settings())) as other:
        assert other.get(BASE).json() == []
        assert other.get("/api/v1/applications").json() == original


@pytest.mark.parametrize("field", ["inputSchema", "outputSchema"])
@pytest.mark.parametrize(
    "schema",
    [
        '{"type":',
        "[]",
        "null",
        '{"value":NaN}',
        '{"type":"nonsense"}',
        '{"required":"name"}',
        '{"properties":[]}',
        '{"$schema":"https://unknown.test/schema"}',
    ],
)
def test_invalid_schema_does_not_save_a_partial_record(client, details, schema, field):
    details[field] = schema
    precheck = client.post("/api/v1/application-precheck", json={"data": details})
    assert field in precheck.json()["fieldErrors"]
    assert client.post(BASE, json={"data": details}).status_code == 422
    assert client.get(BASE).json() == []


@pytest.mark.parametrize("field", ["triggerType", "communicationProtocol", "deployment"])
def test_technical_fields_required_for_submission_but_drafts_are_allowed(client, details, field):
    path = "/api/v1/admin/application-configuration"
    configuration = client.get(path).json()
    descriptor = next(item for item in configuration["fields"] if item["key"] == field)
    assert descriptor["required"] and descriptor["locked"]
    override = [key for key in configuration["requiredFields"] if key != field]
    assert (
        client.put(
            path,
            json=configuration_payload(
                configuration,
                requiredFields=override,
            ),
        ).status_code
        == 422
    )
    details[field] = ""
    draft = client.post(BASE, json={"data": details}).json()
    assert draft["status"] == "draft"
    assert client.post(BASE, json={"data": details, "submit": True}).status_code == 422
    assert (
        client.put(
            f"{BASE}/{draft['id']}",
            json={
                "data": details,
                "submit": True,
                "revision": draft["revision"],
            },
        ).status_code
        == 422
    )


def test_legacy_configuration_keeps_custom_rules_and_enforces_new_mandatory_fields():
    from app.modules.applications.configuration import configuration_view, default_configuration

    stored = {
        **default_configuration(),
        "requiredFields": ["name", "summary", "contact"],
        "fieldHints": {"contact": "负责人"},
        "revision": 9,
    }
    view = configuration_view(stored)
    assert {"triggerType", "communicationProtocol", "deployment", "contact"} <= set(
        view["requiredFields"]
    )
    assert view["revision"] == 9 and view["fieldHints"] == stored["fieldHints"]
    assert stored["requiredFields"] == ["name", "summary", "contact"]


def test_already_pending_submission_keeps_its_frozen_rules(client, details):
    record = client.post(BASE, json={"data": details, "submit": True}).json()
    # 模拟升级前已提交的申请，避免审核时凭新规则锁死旧流程。
    technical = {"triggerType", "communicationProtocol", "deployment"}
    record["submissionRules"]["requiredFields"] = [
        key for key in record["submissionRules"]["requiredFields"] if key not in technical
    ]
    for field in technical:
        record["data"][field] = ""
    with client.app.state.application_service.repository.transaction() as session:
        session.put_publication(record)
    response = client.post(
        f"{ADMIN}/{record['id']}/review",
        json={
            "decision": "approved",
            "revision": record["revision"],
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "dialect",
    [
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2020-12/schema#",
    ],
)
def test_schema_dialects_accept_structure_without_fetching_remote_refs(client, details, dialect):
    details["inputSchema"] = json.dumps(
        {
            "$schema": dialect,
            "type": "object",
            "properties": {
                "q": {"$ref": "https://unavailable.example.test/input.json"},
            },
        }
    )
    assert client.post(BASE, json={"data": details}).status_code == 201


@pytest.mark.parametrize(
    "dependencies",
    [
        [{"kind": "model", "resourceId": "missing-model"}],
        [{"kind": "model", "resourceId": "kb-tech-docs"}],
        [{"kind": "tool", "resourceId": "tool-ocr"}] * 2,
    ],
)
def test_invalid_or_duplicate_dependencies_do_not_save(client, details, dependencies):
    details["dependencies"] = dependencies
    assert client.post(BASE, json={"data": details}).status_code == 422
    assert client.get(BASE).json() == []


def test_dependency_choices_identify_real_sample_resources(client):
    choices = client.get("/api/v1/application-dependencies").json()
    assert {item["kind"] for item in choices} == {"model", "dataset", "knowledge", "tool"}
    assert all(item["resourceId"] and item["name"] and item["owner"] for item in choices)
    assert len(choices) == len({(item["kind"], item["resourceId"]) for item in choices})


def test_new_version_keeps_old_release_until_approval_and_preserves_history(client, details):
    first = client.post(BASE, json={"data": details, "submit": True}).json()
    client.post(
        f"{ADMIN}/{first['id']}/review",
        json={
            "decision": "approved",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
            "revision": 1,
        },
    )
    resource_path = f"/api/v1/applications/{first['applicationId']}"
    previous = client.get(resource_path).json()
    catalog_count = client.get("/api/v1/applications").json()["total"]
    draft = client.post(resource_path + "/version-publications").json()
    assert draft["applicationId"] == first["applicationId"]
    assert draft["versionId"] != first["versionId"]
    assert draft["baseVersionId"] == first["versionId"]
    assert draft["data"]["dependencies"] == details["dependencies"]
    assert draft["data"]["inputSchema"] == details["inputSchema"]
    assert draft["data"]["version"] == ""
    assert client.post(resource_path + "/version-publications").json() == draft
    edit_path = f"{BASE}/{draft['id']}"
    assert client.put(edit_path, json={"data": details, "revision": 1}).status_code == 422
    next_data = {
        **details,
        "version": "v2.4",
        "versionNotes": "增加数据核验",
        "summary": "新版说明",
    }
    pending = client.put(edit_path, json={"data": next_data, "submit": True, "revision": 1}).json()
    assert client.get(resource_path).json() == previous
    review_path = f"{ADMIN}/{draft['id']}/review"
    assert (
        client.post(
            review_path, json={"decision": "needs_changes", "revision": pending["revision"]}
        ).status_code
        == 422
    )
    changes = client.post(
        review_path,
        json={
            "decision": "needs_changes",
            "revision": pending["revision"],
            "reason": "补充输出说明",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
        },
    ).json()
    assert changes["status"] == "needs_changes"
    assert client.get(resource_path).json() == previous
    resubmitted = client.put(
        edit_path, json={"data": next_data, "submit": True, "revision": changes["revision"]}
    ).json()
    approved = client.post(
        review_path,
        json={
            "decision": "approved",
            "scores": {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
            "revision": resubmitted["revision"],
        },
    )
    assert approved.status_code == 200
    current = client.get(resource_path).json()
    assert current["version"] == "v2.4" and current["description"] == "新版说明"
    assert current["versionId"] == draft["versionId"]
    assert current["createdAt"] == previous["createdAt"]
    assert current["usage"] == previous["usage"] and current["rating"] == previous["rating"]
    assert [(item["version"], item["status"]) for item in current["versions"]] == [
        ("v2.4", "current"),
        ("v2.3", "available"),
    ]
    assert current["versions"][0]["note"] == "增加数据核验"
    assert client.get("/api/v1/applications").json()["total"] == catalog_count
    third = client.post(resource_path + "/version-publications").json()
    assert third["id"] != draft["id"] and third["baseVersionId"] == draft["versionId"]
    assert (
        client.put(f"{BASE}/{third['id']}", json={"data": details, "revision": 1}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "sort,first_id",
    [
        ("recent_download", "app-contract"),
        ("downloads", "app-translate"),
        ("favorites", "app-writing"),
    ],
)
def test_yuque_sorting_applies_before_pagination_and_uses_distinct_statistics(
    client, sort, first_id
):
    path = "/api/v1/applications"
    complete = client.get(path, params={"sort": sort, "pageSize": 24}).json()["items"]
    first = client.get(path, params={"sort": sort, "pageSize": 6}).json()["items"]
    second = client.get(path, params={"sort": sort, "pageSize": 6, "page": 2}).json()["items"]
    assert complete[0]["id"] == first_id
    assert first + second == complete
    if sort == "recent_download":
        assert [item["id"] for item in complete[-2:]] == ["app-flow", "app-schedule"]
        assert all(item["lastDownloadedAt"] is None for item in complete[-2:])
    else:
        key = "downloadCount" if sort == "downloads" else "favoriteCount"
        values = [item[key] for item in complete]
        assert values == sorted(values, reverse=True)
    filtered = client.get(
        path,
        params={
            "sort": sort,
            "favoriteIds": json.dumps([complete[-1]["id"], first_id]),
        },
    ).json()
    assert filtered["total"] == 2 and filtered["items"][0]["id"] == first_id


def test_recent_download_compares_instants_and_keeps_missing_records_last(client):
    from app.modules.common.catalog import query_resources
    from app.modules.common.schemas import CatalogQuery

    example = client.get("/api/v1/applications/app-qa").json()
    resources = [
        {**example, "id": "a", "lastDownloadedAt": "2026-09-14T09:00:00+08:00"},
        {**example, "id": "b", "lastDownloadedAt": "2026-09-14T02:00:00Z"},
        {**example, "id": "c", "lastDownloadedAt": None, "updatedAt": "2099-01-01"},
    ]
    assert [
        item["id"]
        for item in query_resources(resources, CatalogQuery(sort="recent_download"))["items"]
    ] == ["b", "a", "c"]


def test_new_version_preserves_download_and_favorite_statistics(client):
    path = "/api/v1/applications/app-contract"
    previous = client.get(path).json()
    draft = client.post(path + "/version-publications").json()
    pending = client.put(
        f"{BASE}/{draft['id']}",
        json={
            "data": {
                **draft["data"],
                "version": "v99.0",
                "triggerType": "手动触发",
                "communicationProtocol": "HTTP",
                "deployment": "工具链托管",
            },
            "submit": True,
            "revision": draft["revision"],
        },
    ).json()
    assert (
        client.post(
            f"{ADMIN}/{draft['id']}/review",
            json={
                "decision": "approved",
                "scores": {
                    "formatCompliance": 4,
                    "safetyCompliance": 4,
                    "annotationCompliance": 4,
                    "structuralCompleteness": 4,
                    "authenticity": 4,
                    "consistency": 4,
                    "cleanliness": 4,
                },
                "revision": pending["revision"],
            },
        ).status_code
        == 200
    )
    current = client.get(path).json()
    for key in ("downloadCount", "favoriteCount", "lastDownloadedAt"):
        assert current[key] == previous[key]


def test_unpublished_application_cannot_start_new_version(client, details):
    draft = client.post(BASE, json={"data": details}).json()
    assert (
        client.post(
            f"/api/v1/applications/{draft['applicationId']}/version-publications"
        ).status_code
        == 404
    )
