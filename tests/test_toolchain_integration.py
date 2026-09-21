"""文档协议和三模块闭环回归：全部请求在本地传输处理，禁止连接真实工具链。"""

import hashlib
import json
from base64 import b64encode
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.main import create_app
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.runtime import AgentDevProvider
from app.modules.common.identity import DEFAULT_USER
from app.modules.common.review_schemas import ReviewQualityScores
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import authorize
from app.modules.knowledge.engine import KnowledgeEngineProvider
from app.modules.knowledge.engine_schemas import IndexInput, RetrievalInput

API = "/api/v1"


@pytest.fixture
def client():
    app = create_app(
        Settings(toolchain_mode="mock", auth_mode="disabled"),
        resources_factory=lambda _: ApplicationResources(),
    )
    with TestClient(app) as client:
        yield client


def scores():
    return ReviewQualityScores(
        **{field: 4 for field in ReviewQualityScores.model_fields}
    ).model_dump(by_alias=True)


def select(client, actor):
    result = client.post(
        API + "/application-demo-session", json={"actorId": actor, "audience": "user"}
    )
    assert result.status_code == 200


def test_pop_signed_json_uses_documented_headers_and_path():
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.url.path == "/gatectl/agent/openapi/agent/pageQuery"
        assert request.headers["x-acs-version"] == "2025-11-10"
        assert request.headers["x-acs-popcode"] == "sfm"
        assert request.headers["x-sfm-workspacecode"] == "test-workspace"
        assert request.headers["authorization"].startswith("acs test-ak:")
        assert (
            request.headers["content-md5"]
            == b64encode(hashlib.md5(request.content).digest()).decode()
        )
        assert json.loads(request.content) == {"current": 1, "agentName": "报告"}
        return httpx.Response(200, json={"success": True, "data": {"list": [], "total": 0}})

    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://supplier.example.test",
        toolchain_workspace="test-workspace",
        toolchain_access_key_id="test-ak",
        toolchain_access_key_secret="test-secret",
    )
    provider = PopClient(settings, httpx.MockTransport(upstream))
    try:
        assert (
            provider.request(
                "POST",
                "/gatectl/agent/openapi/agent/pageQuery",
                action="PostAgentOpenapiAgentPagequery",
                body={"current": 1, "agentName": "报告"},
            )["total"]
            == 0
        )
        assert len(calls) == 1
    finally:
        provider.close()


@pytest.mark.parametrize("failure", ["business", "redirect", "invalid", "timeout", "large"])
def test_pop_failures_are_not_success_or_automatically_retried(failure, caplog):
    calls = []

    def upstream(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("do-not-leak-secret")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example.test"})
        if failure == "invalid":
            return httpx.Response(200, text="do-not-leak-secret")
        if failure == "large":
            return httpx.Response(200, content=b"x" * 8_000_001)
        return httpx.Response(200, json={"success": False, "data": "do-not-leak-secret"})

    client = PopClient(Settings(toolchain_mode="mock"), httpx.MockTransport(upstream))
    try:
        with pytest.raises(HTTPException) as error:
            client.request(
                "POST",
                "/knowledge-engine/api/v1/add",
                action="PostKnowledgeEngineApiV1Add",
                body={},
            )
        assert error.value.status_code == (504 if failure == "timeout" else 502)
        assert len(calls) == 1
        assert "do-not-leak-secret" not in str(error.value.detail) + caplog.text
    finally:
        client.close()


def test_live_workspace_requires_explicit_subject_authorization():
    with pytest.raises(HTTPException) as error:
        authorize(Settings(toolchain_mode="live"), DEFAULT_USER)
    assert error.value.status_code == 403
    authorize(
        Settings(toolchain_mode="live", toolchain_allowed_subjects=[DEFAULT_USER.id]), DEFAULT_USER
    )


@pytest.mark.parametrize(
    "url",
    ["https://user:secret@host.test", "https://host.test/api", "https://host.test/?key=x"],
)
def test_pop_endpoint_rejects_credentials_and_non_origin_urls(url):
    with pytest.raises(ValidationError):
        Settings(toolchain_pop_url=url)


def test_dataset_import_review_and_new_snapshot_preserve_resource_identity(client):
    defaults = client.get(API + "/dataset-publication-defaults").json()
    assert defaults["toolchainAvailable"] and defaults["toolchainMode"] == "mock"
    catalog = client.get(API + "/dataset-toolchain/resources").json()
    identifier = catalog["items"][0]["externalId"]
    detail = client.get(API + "/dataset-toolchain/resources/" + identifier).json()
    assert detail["data"]["dataLevel"] == "" and detail["source"]["externalVersion"] is None
    assert json.loads(detail["data"]["fieldSchema"])["properties"]["value"]["type"] == "number"
    preview = client.get(API + f"/dataset-toolchain/resources/{identifier}/preview").json()
    assert preview["status"] == "BUILT" and preview["rows"]
    payload = {"externalId": identifier, "snapshot": detail["source"]["snapshot"]}
    imported = client.post(API + "/dataset-toolchain/imports", json=payload)
    assert imported.status_code == 200, imported.text
    pub_id = imported.json()["publicationId"]
    path = API + "/dataset-publications/" + pub_id
    record = client.get(path).json()
    assert record["status"] == "draft" and record["source"]["provider"] == "data-engine"
    assert (
        client.post(API + "/dataset-toolchain/imports", json=payload).json()["publicationId"]
        == pub_id
    )
    assert (
        client.put(path, json={"revision": 1, "submit": True, "data": record["data"]}).status_code
        == 422
    )
    data = {
        **record["data"],
        "owner": "研发单位",
        "contact": "发布人",
        "dataApplication": ["试验测试"],
        "dataShape": ["结构化数据"],
        "aiPurpose": "模型训练",
        "language": ["中文"],
        "dataFormat": ["CSV"],
        "dataLevel": "一般数据",
    }
    pending = client.put(path, json={"revision": 1, "submit": True, "data": data})
    assert pending.status_code == 200, pending.text
    approved = client.post(
        API + f"/admin/dataset-publications/{pub_id}/review",
        json={
            "revision": pending.json()["revision"],
            "decision": "approved",
            "scores": scores(),
        },
    )
    assert approved.status_code == 200, approved.text
    client.app.state.toolchain_mock.datasets[0]["description"] = "第二次加工后的资料"
    assert client.post(API + "/dataset-toolchain/imports", json=payload).status_code == 409
    updated = client.get(API + "/dataset-toolchain/resources/" + identifier).json()
    second = client.post(
        API + "/dataset-toolchain/imports",
        json={
            "externalId": identifier,
            "snapshot": updated["source"]["snapshot"],
        },
    ).json()
    next_record = client.get(API + "/dataset-publications/" + second["publicationId"]).json()
    assert next_record["datasetId"] == record["datasetId"]
    assert next_record["data"]["version"] == "v2.0"
    assert client.get(API + "/datasets/" + record["datasetId"]).json()["version"] == "v1.0"
    submitted = client.put(
        API + "/dataset-publications/" + second["publicationId"],
        json={
            "revision": next_record["revision"],
            "submit": True,
            "data": {**data, "version": "v2.0", "summary": next_record["data"]["summary"]},
        },
    )
    assert submitted.status_code == 200, submitted.text
    approved = client.post(
        API + f"/admin/dataset-publications/{second['publicationId']}/review",
        json={
            "revision": submitted.json()["revision"],
            "decision": "approved",
            "scores": scores(),
        },
    )
    assert approved.status_code == 200, approved.text
    latest = client.get(API + "/datasets/" + record["datasetId"]).json()
    assert latest["version"] == "v2.0"
    assert [item["version"] for item in latest["versions"]] == ["v2.0", "v1.0"]
    catalog = client.get(API + "/datasets", params={"pageSize": 100}).json()
    assert sum(item["id"] == record["datasetId"] for item in catalog["items"]) == 1


def test_reader_cannot_read_or_import_service_account_workspace(client):
    select(client, "demo-reader")
    before = len(client.app.state.toolchain_mock.calls)
    assert client.get(API + "/dataset-toolchain/resources").status_code == 403
    assert client.get(API + "/application-toolchain/resources").status_code == 403
    assert (
        client.post(
            API + "/knowledge-bases", json={"name": "库", "description": "简介"}
        ).status_code
        == 403
    )
    assert len(client.app.state.toolchain_mock.calls) == before


def test_application_import_uses_external_version_code_and_missing_fields_remain_blank(client):
    item = client.get(API + "/application-toolchain/resources").json()[0]
    assert item["versions"] == ["20260921001"]
    payload = {
        "externalId": item["externalId"],
        "externalVersion": item["versions"][0],
        "requestId": str(uuid4()),
    }
    response = client.post(API + "/application-toolchain/syncs", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "succeeded", result
    record = client.get(API + "/application-publications/" + result["publicationId"]).json()
    assert record["source"]["provider"] == "agent-dev"
    assert record["source"]["externalVersion"] == "20260921001"
    assert record["source"]["externalStatus"] == "ONLINE"
    assert record["data"]["triggerType"] == ""
    repeated = client.post(
        API + "/application-toolchain/syncs", json={**payload, "requestId": str(uuid4())}
    ).json()
    assert (
        repeated["publicationId"] == result["publicationId"] and repeated["status"] == "unchanged"
    )


def test_runtime_can_bind_approved_import_without_manual_binding_file():
    settings = Settings(
        applications_agent_url="https://runtime.example.test",
        applications_agent_key="test-key",
        toolchain_pop_url="https://pop.example.test",
        toolchain_workspace="workspace",
    )
    provider = AgentDevProvider(DemoApplicationProvider(), settings)
    resource = {
        "id": "platform-app",
        "versionId": "platform-version",
        "source": {
            "provider": "agent-dev",
            "mode": "live",
            "workspace": "workspace",
            "environment": str(settings.toolchain_pop_url),
            "externalId": "agent-code",
            "externalVersion": "version-code",
            "externalStatus": "ONLINE",
        },
    }
    binding = provider.binding(resource)
    assert binding.agent_code == "agent-code" and binding.agent_version == "version-code"
    assert provider.runtime_status(resource)["status"] == "configured"
    resource["source"]["mode"] = "mock"
    assert provider.runtime_status(resource)["status"] == "not_connected"


def create_base(client):
    response = client.post(
        API + "/knowledge-bases", json={"name": "测试知识库", "description": "接口测试"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_knowledge_local_review_does_not_call_external_engine(client):
    """工具链启用后，本地知识库仍须逐层审批，原文和召回不转交另一套知识引擎。"""
    before = len(client.app.state.toolchain_mock.calls)
    base = create_base(client)
    path = API + "/knowledge-bases/" + base["id"]
    admin = API + "/admin/knowledge-bases/" + base["id"]
    body = "试验说明：压力阈值为 100。".encode()
    response = client.post(
        path + "/documents",
        files={"files": ("说明.txt", body, "text/plain")},
        data={"revision": base["revision"]},
    )
    assert response.status_code == 202, response.text
    base = response.json()
    document = base["documents"][0]
    assert document["status"] == "awaiting_review" and base["chunkCount"] == 0
    base = client.post(
        admin + "/review", json={"revision": base["revision"], "decision": "approved"}
    ).json()
    response = client.post(
        admin + "/documents/" + document["id"] + "/review",
        json={"revision": base["revision"], "decision": "approved"},
    )
    assert response.status_code == 200, response.text
    base = client.get(path).json()
    assert base["status"] == "ready" and base["storageBytes"] == len(body)
    assert client.get(path + "/chunks").json()["items"][0]["text"] == body.decode()
    result = client.post(
        path + "/retrieval", json={"query": "压力阈值", "config": base["indexConfig"]}
    )
    assert result.status_code == 200 and result.json()[0]["excerpt"] == body.decode()
    base = client.get(path).json()
    deleted = client.delete(
        path + "/documents", params={"name": document["name"], "revision": base["revision"]}
    )
    assert deleted.status_code == 200 and deleted.json()["documentCount"] == 0
    assert len(client.app.state.toolchain_mock.calls) == before


def test_local_multipart_upload_rejects_unsupported_file():
    """本地文件解析使用 415 拒绝不支持的类型，不登记待审文档。"""
    app = create_app(
        Settings(toolchain_mode="disabled"), resources_factory=lambda _: ApplicationResources()
    )
    with TestClient(app) as client:
        base = create_base(client)
        response = client.post(
            API + f"/knowledge-bases/{base['id']}/documents",
            data={"revision": base["revision"]},
            files={"files": ("invalid.exe", b"invalid", "application/octet-stream")},
        )
        assert response.status_code == 415


def test_knowledge_unauthorized_write_has_no_upstream_side_effect(client):
    base = create_base(client)
    select(client, "demo-outsider")
    before = len(client.app.state.toolchain_mock.calls)
    response = client.post(
        API + f"/knowledge-bases/{base['id']}/documents",
        data={"revision": base["revision"]},
        files={"files": ("secret.txt", b"x", "text/plain")},
    )
    assert response.status_code == 404
    assert len(client.app.state.toolchain_mock.calls) == before


def test_knowledge_retrieval_filters_other_libraries_and_unregistered_documents():
    def upstream(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "dataset_id": "other",
                        "search_result": [{"document_id": "allowed", "text": "secret"}],
                    },
                    {
                        "dataset_id": "current",
                        "search_result": [
                            {
                                "id": "ok",
                                "document_id": "allowed",
                                "text": "public",
                                "chunk_index": 0,
                            },
                            {"id": "bad", "document_id": "private", "text": "secret"},
                        ],
                    },
                ],
            },
        )

    client = PopClient(Settings(toolchain_mode="mock"), httpx.MockTransport(upstream))
    try:
        engine = KnowledgeEngineProvider(client)
        result = engine.retrieve(
            "current",
            [{"id": "allowed", "name": "test.txt", "status": "ready"}],
            RetrievalInput(query="test", config=IndexInput()),
        )
        assert len(result) == 1 and result[0]["excerpt"] == "public" and result[0]["score"] is None
    finally:
        client.close()


def test_unconfirmed_knowledge_configuration_fails_before_external_write():
    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://pop.example.test",
        toolchain_workspace="workspace",
        toolchain_access_key_id="test-ak",
        toolchain_access_key_secret="test-secret",
    )
    calls = []
    client = PopClient(settings, httpx.MockTransport(lambda request: calls.append(request)))
    try:
        with pytest.raises(HTTPException) as error:
            KnowledgeEngineProvider(client).index_options(IndexInput())
        assert error.value.status_code == 422 and not calls
    finally:
        client.close()


def test_knowledge_unknown_status_and_unsupported_operations_are_not_reported_success(client):
    assert KnowledgeEngineProvider.state("") == "parsing"
    assert KnowledgeEngineProvider.state("NEW_UNKNOWN_STATE") == "parsing"
    assert KnowledgeEngineProvider.state("DATA_ITEM_LOADING_FAILED") == "failed"
    base = create_base(client)
    path = API + "/knowledge-bases/" + base["id"]
    assert (
        client.post(
            path + "/documents", json={"files": [{"name": "test.txt", "size": 10}]}
        ).status_code
        == 422
    )
    assert client.post(path + "/rebuild").status_code == 422
    assert client.put(path + "/settings", json=base["indexConfig"]).status_code == 422
