"""独立连接、协议补充和旧配置迁移；全部使用隔离文件及本地 HTTP 传输。"""

import hashlib
import json
from base64 import b64encode
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_toolchain_configuration import PATH, application, payload

from app.core.config import Settings
from app.modules.applications.market_contract import HiMarketClient
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.runtime import AgentDevProvider
from app.modules.common.identity import DEFAULT_USER
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.configuration import ToolchainConfigurationStore, module_settings
from app.modules.common.toolchain.mock import DocumentedToolchainMock
from app.modules.datasets.toolchain import DatasetToolchain
from app.modules.knowledge.engine import KnowledgeEngineProvider
from app.modules.knowledge.schemas import IndexInput, RetrievalInput


def test_module_connections_never_cross_credentials_or_authorization():
    with TestClient(application()) as client:
        data = payload(client)
        data["credentials"] = {}
        for module in ("applications", "datasets", "knowledge"):
            data["configuration"][module]["connection"].update(
                mode="live",
                url=f"https://{module}.invalid",
                workspace=module,
                allowedSubjects=[module + "-user"],
            )
            data["credentials"].update(
                {module + "AccessKeyId": module + "-ak", module + "AccessKeySecret": module + "-sk"}
            )
        assert client.put(PATH, json=data).status_code == 200
    with TestClient(application()) as restarted:
        for module, adapter in restarted.app.state.toolchain_clients.items():
            received = []

            def upstream(request, received=received):
                received.append(request)
                return httpx.Response(200, json={"success": True, "data": {}})

            with closing(PopClient(adapter.settings, httpx.MockTransport(upstream))) as probe:
                probe.request("GET", "/gatectl/catalog", action="ReadCatalog")
            request = received[0]
            assert request.url.host == module + ".invalid"
            assert request.headers["x-sfm-workspacecode"] == module
            assert request.headers["authorization"].startswith("acs " + module + "-ak:")
            assert adapter.settings.toolchain_allowed_subjects == [module + "-user"]


def test_single_enabled_module_does_not_enable_other_modules():
    with TestClient(application()) as client:
        data = payload(client)
        data["configuration"]["datasets"]["connection"]["mode"] = "mock"
        assert client.put(PATH, json=data).status_code == 200
    with TestClient(application()) as client:
        assert client.get(PATH).json()["activeModes"] == {
            "applications": "disabled",
            "datasets": "mock",
            "knowledge": "disabled",
        }
        assert client.get("/api/v1/dataset-toolchain/resources").status_code == 200
        assert client.app.state.toolchain_clients["applications"].mode == "disabled"
        assert not hasattr(client.app.state.knowledge_workspace, "engine")


def test_mock_transport_never_uses_configured_network_proxy(monkeypatch):
    def unexpected_network(*args, **kwargs):
        raise AssertionError("模拟模式不得访问真实网络")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_network)
    settings = Settings(toolchain_mode="mock", toolchain_proxy_url="http://127.0.0.1:1")
    with closing(
        PopClient(
            settings,
            httpx.MockTransport(
                lambda _: httpx.Response(200, json={"success": True, "data": {"mock": True}})
            ),
        )
    ) as client:
        assert client.request("GET", "/gatectl/catalog", action="Catalog") == {"mock": True}


@pytest.mark.parametrize("change", ["certificate", "proxy", "orphan-password"])
def test_invalid_network_or_unbound_password_does_not_create_config(change):
    with TestClient(application()) as client:
        data = payload(client)
        if change == "certificate":
            data["configuration"]["datasets"]["connection"]["caCertificate"] = (
                "never-echo-invalid-cert"
            )
        elif change == "proxy":
            data["configuration"]["applications"]["proxyUrl"] = (
                "http://user:never-echo@proxy.invalid"
            )
        else:
            data["credentials"] = {"marketPassword:missing": "never-echo-password"}
        result = client.put(PATH, json=data)
        assert result.status_code == 422 and "never-echo" not in result.text
        assert not client.app.state.toolchain_configuration.path.exists()


def test_v1_read_migrates_in_memory_without_rewriting_or_losing_credentials(tmp_path):
    fixture = Path(__file__).parent / "fixtures/toolchain-configuration-v1.json"
    document = json.loads(fixture.read_text("utf-8"))
    document["configuration"]["common"].update(mode="mock", workspace="legacy")
    document["credentials"] = {
        "accessKeyId": "old-ak",
        "accessKeySecret": "old-sk",
        "appKey": "runtime-key",
    }
    file = tmp_path / "legacy.json"
    file.write_text(json.dumps(document), encoding="utf-8")
    original = file.read_bytes()
    store = ToolchainConfigurationStore(
        Settings(
            toolchain_configuration_file=file,
            applications_market="himarket",
            applications_market_url="https://old-market.invalid",
            applications_market_data={"profile": "old-market", "bindings": {}},
            applications_market_admin_token="old-market-token",
        )
    )
    assert file.read_bytes() == original
    assert store.active.schema_version == 2
    for module in ("applications", "datasets", "knowledge"):
        assert store.active.credentials[module + "AccessKeySecret"].get_secret_value() == "old-sk"
        assert (
            module_settings(store.settings(store.baseline), module).toolchain_workspace == "legacy"
        )
    assert store.active.credentials["appKey"].get_secret_value() == "runtime-key"
    assert store.active.configuration.knowledge.body_encoding == "json"
    assert "old-sk" not in json.dumps(store.view())
    assert store.active.configuration.applications.market.mode == "himarket"
    assert store.active.credentials["marketAdminToken"].get_secret_value() == "old-market-token"


def test_knowledge_storage_and_tenant_mapping_are_independent(monkeypatch):
    stores = []
    bodies = []
    original = DocumentedToolchainMock.knowledge

    def capture_body(self, request, body):
        bodies.append(body)
        return original(self, request, body)

    monkeypatch.setattr(DocumentedToolchainMock, "knowledge", capture_body)

    class Storage:
        def close(self):
            self.closed = True

    def storage_factory(settings, **network):
        value = Storage()
        stores.append((settings, network, value))
        return value

    monkeypatch.setattr(
        "app.modules.common.toolchain.assembly.MinioStorage.from_settings", storage_factory
    )
    with TestClient(application()) as client:
        data = payload(client)
        knowledge = data["configuration"]["knowledge"]
        knowledge.update(
            ownership="organization",
            tenantBindings=[
                {"organizationId": DEFAULT_USER.organization_id, "tenantId": "vendor-tenant"}
            ],
        )
        knowledge["connection"]["mode"] = "mock"
        knowledge["storage"].update(
            mode="dedicated", endpoint="https://storage.invalid", bucket="kb-documents"
        )
        data["credentials"] = {
            "knowledgeStorageAccessKey": "storage-ak",
            "knowledgeStorageSecretKey": "storage-sk",
        }
        saved = client.put(PATH, json=data)
        assert saved.status_code == 200, saved.text
    with TestClient(application()) as client:
        assert client.app.state.resources.storage is None
        assert client.app.state.knowledge_storage is stores[0][2]
        assert stores[0][0].minio_bucket == "kb-documents"
        created = client.post(
            "/api/v1/knowledge-bases", json={"name": "组织资料", "summary": "组织文件"}
        )
        assert created.status_code == 201, created.text
        assert bodies[-1]["tenantId"] == "vendor-tenant"
        calls = client.app.state.toolchain_mocks["knowledge"].calls
        assert (
            client.app.state.knowledge_workspace.resources[created.json()["id"]]["externalTenantId"]
            == "vendor-tenant"
        )
        client.post(
            "/api/v1/application-demo-session",
            json={"actorId": "demo-admin", "audience": "user"},
        )
        before = len(calls)
        denied = client.post(
            "/api/v1/knowledge-bases", json={"name": "无对应组织", "summary": "组织文件"}
        )
        assert denied.status_code == 422 and len(calls) == before
    assert stores[0][2].closed


def test_pop_body_parameters_use_string_values_in_json_with_valid_digest():
    def upstream(request):
        assert request.headers["content-type"] == "application/json"
        assert request.headers["x-sfm-authorization"] == "Bearer supplementary-key"
        assert (
            request.headers["content-md5"]
            == b64encode(hashlib.md5(request.content).digest()).decode()
        )
        assert json.loads(request.content) == {
            "dataset_ids": '["uuid"]',
            "runInBackground": "true",
            "top_k": "3",
        }
        return httpx.Response(200, json={"success": True, "data": {}})

    with closing(
        PopClient(
            Settings(
                toolchain_mode="mock",
                toolchain_body_encoding="form",
                toolchain_app_key="supplementary-key",
            ),
            httpx.MockTransport(upstream),
        )
    ) as client:
        client.request(
            "POST",
            "/knowledge-engine/api/v1/search",
            action="Search",
            body={"dataset_ids": ["uuid"], "runInBackground": True, "top_k": 3},
        )


@pytest.mark.parametrize("case", ["complete", "partial", "error", "wrong-session"])
def test_runtime_sse_completion_and_session_scope(case):
    def upstream(request):
        assert request.headers["authorization"] == "Bearer runtime-key"
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"success": True, "data": {"uniqueCode": "session-1"}})
        assert json.loads(request.content)["stream"] is True
        events = [{"object": "thought.delta", "content": {"data": "private-thought"}}]
        for text in ("你", "好"):
            events.append(
                {
                    "object": "message.delta",
                    "sessionId": "wrong" if case == "wrong-session" else "session-1",
                    "end": False,
                    "content": [{"type": "text", "text": {"value": text}}],
                }
            )
        if case == "error":
            events.append({"object": "error", "content": {"errorMsg": "private-error"}})
        if case == "complete":
            events.append(
                {"object": "message.delta", "sessionId": "session-1", "end": True, "content": []}
            )
        return httpx.Response(
            200,
            text="".join("data: " + json.dumps(e) + "\n\n" for e in events),
            headers={"Content-Type": "text/event-stream"},
        )

    settings = Settings(
        applications_agent_url="https://runtime.invalid",
        applications_agent_key="runtime-key",
        applications_agent_response_mode="sse",
        applications_agent_bindings_data={
            "app": {"version_id": "v1", "agent_code": "agent", "agent_version": "1"}
        },
    )
    provider = AgentDevProvider(DemoApplicationProvider(), settings, httpx.MockTransport(upstream))
    if case == "complete":
        assert (
            provider.invoke({"id": "app", "versionId": "v1"}, {"question": "hello"}, {}, False)[
                "text"
            ]
            == "你好"
        )
    else:
        with pytest.raises(HTTPException) as error:
            provider.invoke({"id": "app", "versionId": "v1"}, {"question": "hello"}, {}, False)
        assert error.value.status_code == 502
        assert "private" not in str(error.value)


@pytest.mark.parametrize("expiry", [3600, 0, True])
def test_market_developer_login_uses_short_lived_token_without_persisting(expiry):
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/developers/login"):
            assert "authorization" not in request.headers
            assert json.loads(request.content) == {
                "username": "developer",
                "password": "password-private",
            }
            return httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {"accessToken": "temporary-token", "expiresIn": expiry},
                },
            )
        assert request.headers["authorization"] == "Bearer temporary-token"
        return httpx.Response(200, json={"code": "SUCCESS", "data": {"ok": True}})

    settings = Settings(
        applications_market="himarket",
        applications_market_url="https://market.invalid",
        applications_market_data={"profile": "test", "bindings": {}},
        applications_market_accounts={
            "publisher": {"username": "developer", "password": SecretStr("password-private")}
        },
    )
    client = HiMarketClient(settings, httpx.MockTransport(upstream))
    if expiry == 3600:
        assert client.request(
            client.configuration(), "GET", "/me/resources", subject="publisher"
        ) == {"ok": True}
        assert len(calls) == 2 and "temporary-token" not in str(settings)
    else:
        with pytest.raises(HTTPException) as error:
            client.request(client.configuration(), "GET", "/me/resources", subject="publisher")
        assert error.value.status_code == 502 and len(calls) == 1


def test_market_password_is_masked_and_removed_with_account():
    with TestClient(application()) as client:
        data = payload(client)
        data["configuration"]["applications"]["market"]["accounts"] = [
            {"subject": "publisher", "username": "developer"}
        ]
        data["credentials"] = {"marketPassword:publisher": "password-private"}
        result = client.put(PATH, json=data)
        assert result.status_code == 200 and "password-private" not in result.text
        data = payload(client)
        data["configuration"]["applications"]["market"]["accounts"] = []
        assert client.put(PATH, json=data).status_code == 200
        assert not client.app.state.toolchain_configuration.read().credentials


@pytest.mark.parametrize("case", ["valid", "other-file", "expired", "unsafe", "unauthorized"])
def test_dataset_download_checks_file_scope_url_expiry_and_authorization(case):
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "datasetFileInfos": [
                            {"name": "result.csv", "filepath": "dataset/result.csv"}
                        ],
                        "totalCount": 1,
                    },
                },
            )
        assert dict(request.url.params) == {
            "datasetCode": "dataset",
            "filePath": "dataset/result.csv",
            "useShortUrl": "false",
            "RegionId": "",
        }
        expiration = datetime.now(UTC) + timedelta(seconds=-10 if case == "expired" else 3600)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "javascript:alert(1)"
                    if case == "unsafe"
                    else "https://files.invalid/result?signature=temporary",
                    "expiration": 3600,
                    "expireTime": expiration.isoformat(),
                },
            },
        )

    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://dataset.invalid",
        toolchain_workspace="space",
        toolchain_access_key_id="ak",
        toolchain_access_key_secret="sk",
        toolchain_allowed_subjects=[] if case == "unauthorized" else [DEFAULT_USER.id],
    )
    with closing(PopClient(settings, httpx.MockTransport(upstream))) as client:
        service = DatasetToolchain(client, None)
        if case == "valid":
            assert (
                service.download(DEFAULT_USER, "dataset", "dataset/result.csv", 1)["expiration"]
                == 3600
            )
        else:
            with pytest.raises(HTTPException) as error:
                service.download(
                    DEFAULT_USER,
                    "dataset",
                    "other.csv" if case == "other-file" else "dataset/result.csv",
                    1,
                )
            assert error.value.status_code == {"other-file": 404, "unauthorized": 403}.get(
                case, 502
            )
            assert len(calls) == {"other-file": 1, "unauthorized": 0}.get(case, 2)


@pytest.mark.parametrize("failure", ["missing-score", "other-library", "other-document"])
def test_unverified_retrieval_is_blocked_before_call_and_invalid_results_fail(failure):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "dataset_id": "other" if failure == "other-library" else "current",
                        "search_result": [
                            {
                                "document_id": "other" if failure == "other-document" else "file",
                                "text": "content",
                            }
                        ],
                    }
                ],
            },
        )

    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://knowledge.invalid",
        toolchain_workspace="space",
        toolchain_access_key_id="ak",
        toolchain_access_key_secret="sk",
    )
    with closing(PopClient(settings, httpx.MockTransport(upstream))) as client:
        engine = KnowledgeEngineProvider(client)
        payload = RetrievalInput(query="query", config=IndexInput())
        docs = [{"id": "file", "name": "file.txt", "status": "ready"}]
        with pytest.raises(HTTPException) as error:
            engine.retrieve("current", docs, payload)
        assert error.value.status_code == 422 and not calls
        engine.configuration = {
            "retrievalVerified": True,
            "retrievalProfiles": [
                {
                    "confirmed": True,
                    "match": {
                        key: payload.config.model_dump(by_alias=True)[key]
                        for key in ("reranking", "semanticSearch", "keywordSearch")
                    },
                }
            ],
        }
        with pytest.raises(HTTPException) as error:
            engine.retrieve("current", docs, payload)
        assert error.value.status_code == 502 and len(calls) == 1
