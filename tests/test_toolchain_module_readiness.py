"""验证保存配置后的真实协议路径；供应方和存储均在隔离测试传输内运行。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from itertools import product
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_toolchain_configuration import PATH, application, payload
from test_toolchain_integration import scores

from app.core.config import Settings
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.runtime import AgentDevProvider
from app.modules.common.identity import DEFAULT_USER
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.configuration import ToolchainConfigurationStore
from app.modules.common.toolchain.configuration_schemas import (
    IndexProfile,
    KnowledgeConnection,
    SaveToolchainConfiguration,
)
from app.modules.common.toolchain.mock import DocumentedToolchainMock
from app.modules.knowledge.engine import KnowledgeEngineProvider
from app.modules.knowledge.schemas import IndexInput
from app.storage.client import MinioStorage

API = "/api/v1"


def test_first_concurrent_saves_report_conflict_instead_of_filesystem_failure(tmp_path):
    path = tmp_path / "settings.json"
    stores = [
        ToolchainConfigurationStore(Settings(toolchain_configuration_file=path)) for _ in range(12)
    ]
    initial = stores[0].view()
    data = SaveToolchainConfiguration(
        revision=initial["revision"], configuration=initial["configuration"]
    )
    start = Barrier(len(stores))

    def save(store):
        start.wait(timeout=10)
        try:
            store.save(data, "admin")
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(save, stores))
    assert results.count(200) == 1
    assert results.count(409) == len(stores) - 1


def test_imported_dataset_dependencies_follow_publication_visibility_and_listing():
    with TestClient(application(Settings(toolchain_mode="mock"))) as client:
        item = client.get(API + "/dataset-toolchain/resources").json()["items"][0]
        detail = client.get(API + "/dataset-toolchain/resources/" + item["externalId"]).json()
        imported = client.post(
            API + "/dataset-toolchain/imports",
            json={"externalId": item["externalId"], "snapshot": detail["source"]["snapshot"]},
        ).json()
        path = API + "/dataset-publications/" + imported["publicationId"]
        record = client.get(path).json()
        identifier = record["datasetId"]
        assert not any(
            item["resourceId"] == identifier
            for item in client.get(API + "/application-dependencies").json()
        )
        submitted = client.put(
            path,
            json={
                "revision": record["revision"],
                "submit": True,
                "data": {
                    **record["data"],
                    "owner": "研发单位",
                    "contact": "发布人",
                    "dataApplication": ["试验测试"],
                    "dataShape": ["结构化数据"],
                    "aiPurpose": "模型训练",
                    "language": ["中文"],
                    "dataFormat": ["CSV"],
                    "dataLevel": "一般数据",
                },
            },
        )
        assert submitted.status_code == 200, submitted.text
        reviewed = client.post(
            API + "/admin/dataset-publications/" + record["id"] + "/review",
            json={
                "revision": submitted.json()["revision"],
                "decision": "approved",
                "scores": scores(),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        dependency = {"kind": "dataset", "resourceId": identifier}
        assert any(
            item["resourceId"] == identifier
            for item in client.get(API + "/application-dependencies").json()
        )
        draft = {"data": {"name": "关联刚导入的数据", "dependencies": [dependency]}}
        saved = client.post(API + "/application-publications", json=draft)
        assert saved.status_code == 201, saved.text
        admin_path = API + "/admin/datasets/" + identifier
        changed = client.put(
            admin_path + "/visibility",
            json={"scope": "organizations", "targets": ["org-production"], "revision": 0},
        )
        assert changed.status_code == 200, changed.text
        assert not any(
            item["resourceId"] == identifier
            for item in client.get(API + "/application-dependencies").json()
        )
        assert client.post(API + "/application-publications", json=draft).status_code == 422
        assert (
            client.put(
                admin_path + "/visibility",
                json={"scope": "all", "targets": [], "revision": changed.json()["revision"]},
            ).status_code
            == 200
        )
        unlisted = client.put(admin_path + "/listing", json={"status": "unlisted", "revision": 0})
        assert unlisted.status_code == 200, unlisted.text
        assert not any(
            item["resourceId"] == identifier
            for item in client.get(API + "/application-dependencies").json()
        )
        assert client.post(API + "/application-publications", json=draft).status_code == 422


def index_profile(**changes):
    """测试专用供应方字段；仅用于检查透传，不作为现场参数推荐。"""
    return {
        "confirmed": True,
        "dynamicChunking": True,
        "match": {
            "mode": "高质量模式",
            "embeddingModel": "supplier-embedding",
            "chunkMode": "自动分段",
            "chunkSize": 800,
            "chunkOverlap": 120,
            **changes,
        },
        "pipelineOptions": {"testOnly": {"size": "${chunkSize}", "overlap": "${chunkOverlap}"}},
    }


@pytest.mark.parametrize("size,overlap", [(100, 0), (500, 499), (2000, 500)])
def test_dynamic_index_preserves_user_numbers_and_leaves_configuration_intact(size, overlap):
    profile = IndexProfile.model_validate(index_profile()).model_dump(by_alias=True)
    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://kb.invalid",
        toolchain_workspace="kb",
        toolchain_access_key_id="test-ak",
        toolchain_access_key_secret="test-sk",
        toolchain_knowledge_data={"indexProfiles": [profile]},
    )
    with closing(
        PopClient(settings, httpx.MockTransport(lambda _: pytest.fail("仅转换参数")))
    ) as client:
        provider = KnowledgeEngineProvider(client)
        actual = provider.index_options(
            IndexInput(embedding_model="supplier-embedding", chunk_size=size, chunk_overlap=overlap)
        )
    assert actual == {"testOnly": {"size": size, "overlap": overlap}}
    assert profile["pipelineOptions"]["testOnly"]["size"] == "${chunkSize}"


@pytest.mark.parametrize("case", ["missing", "unknown", "expression", "fixed", "overlap"])
def test_ambiguous_or_incomplete_parameter_mapping_is_rejected(case):
    profile = index_profile()
    if case == "missing":
        profile["pipelineOptions"] = {"size": "${chunkSize}"}
    elif case == "unknown":
        profile["pipelineOptions"] = {"size": "${secret}"}
    elif case == "expression":
        profile["pipelineOptions"] = {"size": "${chunkSize} / 2"}
    elif case == "fixed":
        profile["dynamicChunking"] = False
    with pytest.raises(ValidationError):
        if case == "overlap":
            KnowledgeConnection(index_profiles=[profile, index_profile(chunkSize=1000)])
        else:
            IndexProfile.model_validate(profile)


def test_manual_binding_does_not_disable_other_imported_apps_and_stale_binding_fails():
    settings = Settings(
        applications_agent_url="https://agent.invalid",
        applications_agent_key="test-runtime",
        toolchain_workspace="apps",
        toolchain_pop_url="https://agent.invalid",
        applications_agent_bindings_data={
            "manual": {
                "version_id": "v1",
                "agent_code": "other",
                "agent_version": "2",
                "input_mode": "text",
            }
        },
    )
    provider = AgentDevProvider(DemoApplicationProvider(), settings)
    resource = {
        "id": "imported",
        "versionId": "v1",
        "source": {
            "provider": "agent-dev",
            "mode": "live",
            "workspace": "apps",
            "environment": str(settings.toolchain_pop_url),
            "externalId": "agent-1",
            "externalVersion": "3",
            "externalStatus": "ONLINE",
        },
    }
    assert provider.binding(resource).agent_code == "agent-1"
    resource.update(id="manual", versionId="v2")
    with pytest.raises(HTTPException):
        provider.binding(resource)
    resource["id"] = "imported"
    resource["source"]["workspace"] = "another-workspace"
    with pytest.raises(HTTPException):
        provider.binding(resource)


def test_checks_distinguish_complete_configuration_from_missing_supplier_contract():
    with TestClient(application()) as client:
        data = payload(client)
        config = data["configuration"]
        data["credentials"] = {
            "knowledgeAccessKeyId": "test-ak",
            "knowledgeAccessKeySecret": "test-sk",
        }
        config["knowledge"]["connection"].update(
            mode="live", url="https://kb.invalid", workspace="kb", allowedSubjects=[DEFAULT_USER.id]
        )
        config["knowledge"]["indexProfiles"] = [
            index_profile(mode=mode, chunkMode=chunk)
            for mode, chunk in product(
                ("高质量模式", "经济模式"), ("自动分段", "按段落", "固定长度")
            )
        ]
        config["knowledge"]["retrievalProfiles"] = [
            {
                "confirmed": True,
                "match": {
                    "reranking": rerank,
                    "semanticSearch": semantic,
                    "keywordSearch": keyword,
                },
                "retrieverSpecificConfig": {"testOnly": [rerank, semantic, keyword]},
            }
            for rerank, semantic, keyword in product((False, True), repeat=3)
            if semantic or keyword
        ]
        result = client.put(PATH, json=data)
        assert result.status_code == 200, result.text
        checks = result.json()["readiness"]["knowledge"]
        assert checks["configurationComplete"] is False
        statuses = {item["code"]: item["status"] for item in checks["checks"]}
        assert statuses["knowledge-index"] == "configured"
        assert statuses["knowledge-retrieval-options"] == "configured"
        assert statuses["knowledge-storage"] == "missing"
        assert statuses["knowledge-retrieval-contract"] == "supplier"
        assert result.json()["activeModes"]["knowledge"] == "disabled"
        assert client.get(API + "/knowledge-bases/options").json()["mode"] == "disabled"


def test_saved_live_connections_drive_three_module_workflows_after_restart(monkeypatch):
    upstream = DocumentedToolchainMock()
    calls = []
    original_init = PopClient.__init__
    has_score = True

    def transport(request):
        calls.append(request)
        if request.url.path.endswith("/files/presigned/url"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "url": "https://files.invalid/result.csv?signature=test-only",
                        "expiration": 60,
                        "expireTime": (datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                    },
                },
            )
        response = upstream(request)
        if request.url.path.endswith("/search") and has_score:
            body = response.json()
            for group in body["data"]:
                for result in group["search_result"]:
                    result["score"] = 0.9
            return httpx.Response(200, json=body)
        return response

    def init(self, settings, supplied_transport=None):
        original_init(self, settings, httpx.MockTransport(transport))

    class Storage:
        def __init__(self):
            self.files = {}

        def put_object(self, key, content, **kwargs):
            self.files[key] = content.read()

        def presigned_read_url(self, key, **kwargs):
            return upstream.store_file(key.rsplit("/", 1)[-1], self.files[key])

        def close(self):
            pass

    monkeypatch.setattr(PopClient, "__init__", init)
    monkeypatch.setattr(MinioStorage, "from_settings", lambda *args, **kwargs: Storage())
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("禁止真实网络")
    )
    with TestClient(application()) as client:
        data = payload(client)
        data["credentials"] = {
            "appKey": "test-runtime",
            "knowledgeStorageAccessKey": "store-ak",
            "knowledgeStorageSecretKey": "store-sk",
        }
        config = data["configuration"]
        for module in ("applications", "datasets", "knowledge"):
            config[module]["connection"].update(
                mode="live",
                url=f"https://{module}.invalid",
                workspace=module,
                allowedSubjects=[DEFAULT_USER.id],
            )
            data["credentials"].update(
                {module + "AccessKeyId": module + "-ak", module + "AccessKeySecret": module + "-sk"}
            )
        config["applications"].update(runtime="agent_dev", url=None)
        config["knowledge"].update(
            indexProfiles=[index_profile()],
            retrievalVerified=True,
            retrievalProfiles=[
                {
                    "confirmed": True,
                    "match": {"reranking": True, "semanticSearch": True, "keywordSearch": True},
                    "retrieverSpecificConfig": {},
                }
            ],
        )
        config["knowledge"]["storage"].update(mode="dedicated", endpoint="https://files.invalid")
        result = client.put(PATH, json=data)
        assert result.status_code == 200, result.text

    with TestClient(application()) as client:
        assert not client.get(PATH).json()["restartRequired"]
        assert client.app.state.database_engine is None
        agent = client.get(API + "/application-toolchain/resources").json()[0]
        imported = client.post(
            API + "/application-toolchain/syncs",
            json={
                "externalId": agent["externalId"],
                "externalVersion": agent["versions"][0],
                "requestId": str(uuid4()),
            },
        ).json()
        assert imported["status"] == "succeeded"
        pub_path = API + "/application-publications/" + imported["publicationId"]
        publication = client.get(pub_path).json()
        assert not publication["data"]["triggerType"]
        submitted = client.put(
            pub_path,
            json={
                "revision": publication["revision"],
                "submit": True,
                "data": {
                    **publication["data"],
                    "triggerType": "手动触发",
                    "communicationProtocol": "HTTP",
                    "deployment": "工具链托管",
                    "owner": "研发单位",
                },
            },
        )
        assert submitted.status_code == 200, submitted.text
        approved = client.post(
            API + "/admin/application-publications/" + imported["publicationId"] + "/review",
            json={
                "revision": submitted.json()["revision"],
                "decision": "approved",
                "scores": scores(),
            },
        )
        assert approved.status_code == 200, approved.text
        resource = client.app.state.application_service.resource(publication["applicationId"])
        runtime = client.app.state.application_service.provider
        assert str(runtime.settings.applications_agent_url) == "https://applications.invalid/"
        runtime_calls = []

        def run_transport(request):
            runtime_calls.append(request)
            assert request.headers["authorization"] == "Bearer test-runtime"
            assert request.url.host == "applications.invalid"
            if request.url.path.endswith("/createSession"):
                return httpx.Response(
                    200, json={"success": True, "data": {"uniqueCode": "session-1"}}
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "message": {
                            "content": [{"type": "text", "text": {"value": "供应方测试结果"}}]
                        }
                    },
                },
            )

        runtime.transport = httpx.MockTransport(run_transport)
        assert runtime.invoke(resource, {"question": "测试"}, {}, False)["text"] == "供应方测试结果"
        assert len(runtime_calls) == 2
        dataset = client.get(API + "/dataset-toolchain/resources").json()["items"][0]
        dataset_path = API + "/dataset-toolchain/resources/" + dataset["externalId"]
        detail = client.get(dataset_path).json()
        assert client.get(dataset_path + "/preview").json()["status"] == "BUILT"
        assert client.get(dataset_path + "/files").json()["items"][0]["filepath"] == "demo.csv"
        assert (
            client.get(dataset_path + "/download", params={"filePath": "demo.csv"}).status_code
            == 200
        )
        assert (
            client.post(
                API + "/dataset-toolchain/imports",
                json={
                    "externalId": dataset["externalId"],
                    "snapshot": detail["source"]["snapshot"],
                },
            ).status_code
            == 200
        )
        options = client.get(API + "/knowledge-bases/options").json()
        assert options["indexProfiles"][0]["match"]["embeddingModel"] == "supplier-embedding"
        assert "pipelineOptions" not in str(options)
        index = IndexInput(
            embedding_model="supplier-embedding", chunk_size=1300, chunk_overlap=300
        ).model_dump(by_alias=True)
        created = client.post(
            API + "/knowledge-bases",
            json={"name": "真实协议测试", "summary": "本地传输", "indexConfig": index},
        )
        assert created.status_code == 201, created.text
        path = API + "/knowledge-bases/" + created.json()["id"]
        result = client.post(
            path + "/files",
            files={"files": ("说明.txt", "测试正文".encode())},
            data={"config": json.dumps(index)},
        )
        assert result.status_code == 200, result.text
        status = client.get(path + "/status").json()
        assert status["status"] == "ready" and status["indexBuiltAt"]
        assert client.get(path + "/status").json()["indexBuiltAt"] == status["indexBuiltAt"]
        assert client.get(path + "/chunks").json()["items"][0]["text"] == "测试正文"
        retrieval = {"query": "测试", "config": index}
        assert client.post(path + "/retrieval", json=retrieval).json()[0]["score"] == 0.9
        has_score = False
        assert client.post(path + "/retrieval", json=retrieval).status_code == 502
        current = client.app.state.knowledge_workspace.resources[created.json()["id"]]
        assert current["retrievalCount"] == 2 and current["retrievalSuccessRate"] == 50
        assert current["averageRetrievalMs"] > 0
        assert (
            client.delete(
                path + "/documents", params={"name": status["documents"][0]["id"]}
            ).status_code
            == 200
        )
        assert client.get(path + "/status").json()["documentCount"] == 0

    for request in calls:
        module = request.url.host.split(".")[0]
        assert request.headers["x-sfm-workspacecode"] == module
        assert request.headers["authorization"].startswith("acs " + module + "-ak:")
    create_call = next(
        call
        for call in calls
        if call.method == "POST" and call.url.path == "/knowledge-engine/api/v1/datasets"
    )
    assert json.loads(json.loads(create_call.content)["pipelineOptions"]) == {
        "testOnly": {"size": 1300, "overlap": 300}
    }
