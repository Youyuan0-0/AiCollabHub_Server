"""华为查询、脱敏、幂等导入和定期轮询验证；不连接真实服务。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_toolchain_configuration import PATH, application, payload

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.main import create_app
from app.modules.common.identity import DEFAULT_USER
from app.modules.common.toolchain.catalog import ToolchainCatalogs
from app.modules.common.toolchain.catalog_store import CatalogStore, catalog_table
from app.modules.common.toolchain.configuration import module_settings
from app.modules.common.toolchain.huawei import (
    AGENT_DETAIL,
    CATALOG_PATHS,
    HuaweiCatalog,
    HuaweiClient,
)
from app.modules.common.toolchain.huawei_mock import HuaweiToolchainMock

API = "/api/v1/external-toolchain"
MODULES = tuple(CATALOG_PATHS)


@pytest.fixture
def client():
    settings = Settings(
        toolchain_provider="huawei",
        toolchain_mode="mock",
        toolchain_models_mode="mock",
        toolchain_polling_enabled=False,
        auth_mode="disabled",
    )
    app = create_app(settings, resources_factory=lambda _: ApplicationResources())
    with TestClient(app) as session:
        yield session


def test_all_four_queries_are_sanitized_and_separate_from_business_resources(client):
    for module in MODULES:
        config = client.get(f"{API}/{module}/configuration").json()
        assert config["provider"] == "huawei" and config["available"]
        assert config["importAvailable"] == (module in ("applications", "datasets"))
        pending = client.get(f"{API}/{module}/resources").json()
        assert pending["status"] == "pending" and pending["lastSyncedAt"] is None
        refreshed = client.post(f"{API}/{module}/refresh").json()
        assert refreshed["status"] == "ready" and refreshed["lastSyncedAt"]
        page = client.get(f"{API}/{module}/resources").json()
        assert page["total"] == 1
        row = page["items"][0]
        detail = client.get(f"{API}/{module}/resources/{row['externalId']}")
        assert detail.status_code == 200
        assert detail.json()["externalId"] == row["externalId"]
        assert "mock-hidden-key" not in detail.text + json.dumps(page)
        assert not ({"config", "engine_config", "data", "file_id"} & detail.json().keys())
        assert client.get(f"{API}/{module}/resources?q=never-matches").json()["total"] == 0
    assert client.get("/api/v1/dataset-publications").json() == []
    assert client.get("/api/v1/application-publications").json() == []
    assert client.get("/api/v1/model-toolchain").status_code == 503


@pytest.mark.parametrize("module", ["applications", "datasets"])
def test_import_is_idempotent_uses_platform_uuid_and_preserves_external_version(client, module):
    client.post(f"{API}/{module}/refresh")
    row = client.get(f"{API}/{module}/resources").json()["items"][0]
    detail_url = f"{API}/{module}/resources/{row['externalId']}"
    detail = client.get(detail_url).json()
    request = {"snapshot": detail["snapshot"]}
    imported = client.post(detail_url + "/import", json=request)
    assert imported.status_code == 200, imported.text
    publication_id = imported.json()["publicationId"]
    assert UUID(publication_id)
    duplicate = client.post(detail_url + "/import", json=request)
    assert duplicate.status_code == 200 and duplicate.json()["publicationId"] == publication_id
    prefix = "application" if module == "applications" else "dataset"
    records = client.get(f"/api/v1/{prefix}-publications").json()
    assert len(records) == 1
    assert records[0]["status"] == "draft"
    source = records[0]["source"]
    assert source["provider"] == "huawei-platform" and source["workspace"] == ""
    assert source["externalVersion"] == detail["externalVersion"]
    assert source["snapshot"] == detail["snapshot"]
    assert records[0]["data"]["version"] != detail["externalVersion"]
    client.app.state.huawei_toolchain_mocks[module].rows[module][0]["description"] = (
        "已修改的外部资料"
    )
    stale = client.post(detail_url + "/import", json=request)
    assert stale.status_code == 409
    latest = client.get(detail_url).json()
    # 尚有未完成草稿时，新的来源快照不能覆盖人工编辑资料。
    assert (
        client.post(detail_url + "/import", json={"snapshot": latest["snapshot"]}).status_code
        == 409
    )
    assert len(client.get(f"/api/v1/{prefix}-publications").json()) == 1


def test_documents_ownership_and_pageindex_exclusion(client):
    path = f"{API}/knowledge/resources/hw-knowledge-1/documents"
    result = client.get(path)
    assert result.status_code == 200
    item = result.json()["items"][0]
    assert item["statusLabel"] == "可用" and item["segmentCount"] == 32
    assert "external-file-1" not in result.text
    client.app.state.huawei_toolchain_mocks["knowledge"].rows["knowledge"][0]["data"]["type"] = (
        "PageIndex"
    )
    assert client.get(path).status_code == 422
    calls = []

    def wrong_library(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "total": 1,
                    "data": [
                        {"id": "doc-1", "name": "文档", "knowledge_id": "other", "status": 4},
                    ],
                },
            },
        )

    with closing(
        HuaweiClient(Settings(toolchain_mode="mock"), httpx.MockTransport(wrong_library))
    ) as probe:
        with pytest.raises(HTTPException, match="其他知识库"):
            HuaweiCatalog(probe).documents("mine", 1, 20)
    assert json.loads(calls[0].content) == {"knowledge_id": "mine", "page": 1, "size": 20}


def test_directory_access_checks_role_and_live_authorized_subject(client):
    client.post(
        "/api/v1/application-demo-session", json={"actorId": "demo-reader", "audience": "user"}
    )
    assert client.get(f"{API}/models/resources").status_code == 403
    assert client.post(f"{API}/models/refresh").status_code == 403
    client.post(
        "/api/v1/application-demo-session", json={"actorId": DEFAULT_USER.id, "audience": "user"}
    )
    specific = client.app.state.settings.toolchain_module_connections.setdefault("models", {})
    specific.update(toolchain_mode="live", toolchain_allowed_subjects=["someone-else"])
    assert client.get(f"{API}/models/resources").status_code == 403
    specific["toolchain_allowed_subjects"] = [DEFAULT_USER.id]
    assert client.get(f"{API}/models/resources").status_code == 200


@pytest.mark.parametrize("failure", ["auth", "code", "redirect", "invalid", "timeout", "large"])
def test_client_contract_and_failures_do_not_leak_or_retry(failure):
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == AGENT_DETAIL
        assert request.headers["api-key"] == "private-api-key"
        assert json.loads(request.content) == {"id": "agent-1"}
        if failure == "auth":
            return httpx.Response(401, text="private-api-key")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.invalid"})
        if failure == "invalid":
            return httpx.Response(200, text="private-api-key")
        if failure == "timeout":
            raise httpx.ReadTimeout("private-api-key")
        if failure == "large":
            return httpx.Response(200, content=b"x" * 8_000_001)
        return httpx.Response(200, json={"code": "200", "message": "private-api-key", "data": {}})

    settings = Settings(
        toolchain_mode="live",
        toolchain_pop_url="https://provider.invalid",
        toolchain_api_key="private-api-key",
    )
    with closing(HuaweiClient(settings, httpx.MockTransport(upstream))) as probe:
        with pytest.raises(HTTPException) as caught:
            probe.request(AGENT_DETAIL, {"id": "agent-1"})
        assert caught.value.status_code == (504 if failure == "timeout" else 502)
        assert "private-api-key" not in str(caught.value.detail)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, "duplicate", "total-change", "incomplete"])
def test_full_pagination_and_rejected_partial_results(failure):
    pages = []

    def upstream(request):
        body = json.loads(request.content)
        page = body["page"]
        pages.append(page)
        assert body["size"] == 100
        rows = [
            {"id": str(i), "name": f"资源{i}"}
            for i in range((page - 1) * 100, min(page * 100, 102))
        ]
        if page == 2 and failure == "duplicate":
            rows[0]["id"] = "0"
        if page == 2 and failure == "incomplete":
            rows = []
        total = 103 if page == 2 and failure == "total-change" else 102
        return httpx.Response(200, json={"code": 200, "data": {"total": total, "data": rows}})

    with closing(
        HuaweiClient(Settings(toolchain_mode="mock"), httpx.MockTransport(upstream))
    ) as probe:
        if failure:
            with pytest.raises(HTTPException):
                HuaweiCatalog(probe).fetch("models", lambda: None)
        else:
            assert len(HuaweiCatalog(probe).fetch("models", lambda: None)) == 102
    assert pages == [1, 2]


@pytest.mark.parametrize("persistent", [False, True])
def test_leases_persisted_snapshot_backoff_and_stale_worker(persistent, tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'catalog.db'}") if persistent else None
    if engine:
        catalog_table().metadata.create_all(engine)
    store = CatalogStore(engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(lambda _: store.claim("key", "models", 120), range(4)))
    assert sum(token is not None for token in tokens) == 1
    token = next(token for token in tokens if token)
    store.renew("key", token, 120)
    store.finish("key", token, 300, items=[{"externalId": "previous"}])
    assert store.claim("key", "models", 120) is None
    second = store.claim("key", "models", 120, force=True)
    store.finish("key", second, 300, error="查询失败")
    row = store.load("key")
    assert row["data"]["items"] == [{"externalId": "previous"}]
    assert row["failure_count"] == 1 and row["last_error"] == "查询失败"
    assert row["next_poll_at"] > datetime.now(UTC) + timedelta(seconds=590)
    third = store.claim("key", "models", 120, force=True)
    store.finish("key", second, 300, items=[])
    assert store.load("key")["lease_token"] == third
    store.finish("key", third, 300, items=[])
    assert store.load("key")["data"]["items"] == []
    if engine:
        assert CatalogStore(engine).load("key")["last_synced_at"]
        engine.dispose()


def test_refresh_retains_previous_snapshot_and_connection_scope(client):
    catalogs = client.app.state.toolchain_catalogs
    before = catalogs.refresh("models", force=True)
    assert before["status"] == "ready"
    client.app.state.huawei_toolchain_mocks["models"].rows["models"][0].pop("name")
    failed = catalogs.refresh("models", force=True)
    assert failed["status"] == "error" and failed["items"] == before["items"]
    assert failed["lastSyncedAt"] == before["lastSyncedAt"]
    probe = catalogs.clients["models"]
    old_key = catalogs.connection_key("models")
    probe.settings = probe.settings.model_copy(
        update={"toolchain_pop_url": "https://other.invalid"}
    )
    assert catalogs.connection_key("models") != old_key
    assert catalogs.state("models")["status"] == "pending"


@pytest.mark.parametrize("module", ["applications", "datasets"])
def test_import_recovers_binding_after_runtime_cache_expiry(client, module):
    catalogs = client.app.state.toolchain_catalogs
    row = catalogs.refresh(module, force=True)["items"][0]
    url = f"{API}/{module}/resources/{row['externalId']}"
    snapshot = client.get(url).json()["snapshot"]
    imported = client.post(url + "/import", json={"snapshot": snapshot}).json()
    if module == "applications":
        with client.app.state.application_service.repository.transaction() as session:
            session.state["activities"]["bindings"] = {}
    else:
        repository = client.app.state.dataset_workspace.repository
        for key in list(repository._records):
            if key.startswith("toolchain:"):
                del repository._records[key]
    repeated = client.post(url + "/import", json={"snapshot": snapshot})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["publicationId"] == imported["publicationId"]
    client.app.state.huawei_toolchain_mocks[module].rows[module][0]["description"] = (
        "过期后的新资料"
    )
    latest = client.get(url).json()["snapshot"]
    assert client.post(url + "/import", json={"snapshot": latest}).status_code == 409


def test_background_polling_respects_switch_and_stops():
    mock = HuaweiToolchainMock()
    with closing(
        HuaweiClient(Settings(toolchain_mode="mock"), httpx.MockTransport(mock))
    ) as enabled:
        with closing(
            HuaweiClient(
                Settings(toolchain_mode="mock", toolchain_polling_enabled=False),
                httpx.MockTransport(mock),
            )
        ) as disabled:
            catalogs = ToolchainCatalogs({"models": enabled, "knowledge": disabled}, CatalogStore())

            async def exercise():
                task = asyncio.create_task(catalogs.maintain())
                for _ in range(100):
                    if catalogs.state("models")["status"] == "ready":
                        break
                    await asyncio.sleep(0.01)
                catalogs.stop()
                await asyncio.wait_for(task, 1)

            asyncio.run(exercise())
            assert catalogs.state("models")["status"] == "ready"
            assert catalogs.state("knowledge")["status"] == "pending"


def test_saved_huawei_configuration_uses_api_keys_without_aliyun_workspace():
    with TestClient(application()) as session:
        data = payload(session)
        for module in MODULES:
            data["configuration"][module]["connection"].update(
                provider="huawei",
                mode="live",
                url="https://provider.invalid",
                allowedSubjects=[DEFAULT_USER.id],
                pollingEnabled=False,
                pollInterval=600,
            )
        missing = session.put(PATH, json=data)
        assert missing.status_code == 422
        data["credentials"] = {module + "ApiKey": "private-key" for module in MODULES}
        saved = session.put(PATH, json=data)
        assert saved.status_code == 200, saved.text
        assert "private-key" not in saved.text
    with TestClient(application()) as restarted:
        for module in MODULES:
            probe = restarted.app.state.huawei_toolchain_clients[module]
            assert isinstance(probe, HuaweiClient)
            assert probe.settings.toolchain_poll_interval == 600
            assert probe.settings.toolchain_api_key.get_secret_value() == "private-key"
        assert (
            restarted.app.state.toolchain_clients["tools"].settings.toolchain_provider == "aliyun"
        )


def test_missing_catalog_table_fails_without_implicit_creation():
    engine = sa.create_engine("sqlite://")
    try:
        with pytest.raises(RuntimeError, match="迁移"):
            CatalogStore(engine)
        assert sa.inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_both_providers_save_and_restart_with_separate_credentials_and_workbenches():
    """后台同时保存两家连接，页面查询两家不会变更已保存配置。"""
    with TestClient(application()) as session:
        data = payload(session)
        for module in MODULES:
            data["configuration"][module]["connection"].update(
                mode="mock",
                url="https://aliyun.invalid",
                workspace="ali-space",
                region="ali-region",
            )
            data["configuration"]["huawei"][module]["connection"].update(
                mode="mock",
                url="https://huawei.invalid",
                pollingEnabled=False,
            )
        data["configuration"]["datasets"]["entryUrl"] = "https://aliyun.invalid/workbench"
        data["configuration"]["huawei"]["datasets"]["entryUrl"] = "https://huawei.invalid/workbench"
        data["credentials"] = {
            "datasetsAccessKeyId": "ali-private-id",
            "datasetsAccessKeySecret": "ali-private-secret",
            "datasetsApiKey": "huawei-private-key",
        }
        saved = session.put(PATH, json=data)
        assert saved.status_code == 200, saved.text
        assert all(value not in saved.text for value in data["credentials"].values())
    with TestClient(application()) as restarted:
        settings = restarted.app.state.settings
        aliyun = module_settings(settings, "datasets", "aliyun")
        huawei = module_settings(settings, "datasets", "huawei")
        assert aliyun.toolchain_access_key_id.get_secret_value() == "ali-private-id"
        assert aliyun.toolchain_access_key_secret.get_secret_value() == "ali-private-secret"
        assert huawei.toolchain_api_key.get_secret_value() == "huawei-private-key"
        assert huawei.toolchain_access_key_id is None and huawei.toolchain_access_key_secret is None
        path = restarted.app.state.toolchain_configuration.path
        before = path.read_bytes()
        for module in MODULES:
            states = restarted.get(f"{API}/{module}/providers").json()
            assert states["aliyun"]["available"] and states["huawei"]["available"]
            assert "private" not in json.dumps(states)
        assert restarted.get("/api/v1/application-toolchain/resources").status_code == 200
        assert restarted.get("/api/v1/dataset-toolchain/resources").status_code == 200
        assert restarted.get("/api/v1/model-toolchain").status_code == 200
        assert restarted.post(f"{API}/applications/refresh").status_code == 200
        assert restarted.get("/api/v1/dataset-publication-defaults").json()["toolchainUrl"] == (
            "https://aliyun.invalid/workbench"
        )
        assert restarted.get(f"{API}/datasets/configuration").json()["entryUrl"] == (
            "https://huawei.invalid/workbench"
        )
        assert path.read_bytes() == before


def test_same_dataset_id_from_two_providers_creates_distinct_idempotent_drafts():
    """供应方属于来源键，相同外部编号不能合并成同一份平台申请。"""
    settings = Settings(
        toolchain_mode="mock",
        auth_mode="disabled",
        toolchain_provider_connections={
            "datasets": {
                "huawei": {
                    "toolchain_provider": "huawei",
                    "toolchain_mode": "mock",
                    "toolchain_polling_enabled": False,
                }
            },
        },
    )
    with TestClient(
        create_app(settings, resources_factory=lambda _: ApplicationResources())
    ) as session:
        aliyun = session.get("/api/v1/dataset-toolchain/resources").json()["items"][0]
        identifier = aliyun["externalId"]
        session.app.state.huawei_toolchain_mocks["datasets"].rows["datasets"][0]["id"] = identifier
        session.post(f"{API}/datasets/refresh")
        ali_detail = session.get(f"/api/v1/dataset-toolchain/resources/{identifier}").json()
        hw_url = f"{API}/datasets/resources/{identifier}"
        hw_detail = session.get(hw_url).json()
        ali_payload = {"externalId": identifier, "snapshot": ali_detail["source"]["snapshot"]}
        hw_payload = {"snapshot": hw_detail["snapshot"]}
        ali_import = session.post("/api/v1/dataset-toolchain/imports", json=ali_payload)
        hw_import = session.post(hw_url + "/import", json=hw_payload)
        assert ali_import.status_code == hw_import.status_code == 200
        ali_id, hw_id = ali_import.json()["publicationId"], hw_import.json()["publicationId"]
        assert ali_id != hw_id
        assert (
            session.post("/api/v1/dataset-toolchain/imports", json=ali_payload).json()[
                "publicationId"
            ]
            == ali_id
        )
        assert session.post(hw_url + "/import", json=hw_payload).json()["publicationId"] == hw_id
        records = session.get("/api/v1/dataset-publications").json()
        assert len(records) == 2
        assert {item["source"]["provider"] for item in records} == {
            "data-engine",
            "huawei-platform",
        }
        assert {item["source"]["externalId"] for item in records} == {identifier}


@pytest.mark.parametrize("restricted", ["aliyun", "huawei"])
def test_provider_permissions_are_independent_without_changing_running_clients(restricted):
    """一家的授权不允许访问另一家；页面查询不会替换正在运行的客户端。"""
    connections = {
        provider: {
            "toolchain_provider": provider,
            "toolchain_mode": "live" if restricted == provider else "mock",
            "toolchain_polling_enabled": False,
            "toolchain_pop_url": "https://supplier.invalid",
            "toolchain_workspace": "space",
            "toolchain_allowed_subjects": ["someone-else"],
            "toolchain_api_key": SecretStr("private-api-key") if provider == "huawei" else None,
            "toolchain_access_key_id": SecretStr("private-id") if provider == "aliyun" else None,
            "toolchain_access_key_secret": SecretStr("private-secret")
            if provider == "aliyun"
            else None,
        }
        for provider in ("aliyun", "huawei")
    }
    settings = Settings(
        toolchain_mode="mock",
        auth_mode="disabled",
        toolchain_provider_connections={"datasets": connections},
    )
    with TestClient(
        create_app(settings, resources_factory=lambda _: ApplicationResources())
    ) as session:
        clients = session.app.state.toolchain_clients
        original = clients["datasets"]
        assert session.get("/api/v1/dataset-toolchain/resources").status_code == (
            403 if restricted == "aliyun" else 200
        )
        expected = 403 if restricted == "huawei" else 200
        assert session.get(f"{API}/datasets/resources").status_code == expected
        assert session.post(f"{API}/datasets/refresh").status_code == expected
        assert clients["datasets"] is original


def test_aliyun_runtime_does_not_read_legacy_huawei_connection():
    """旧华为环境仍可查询，阿里应用运行必须读取自己的工作空间。"""
    settings = Settings(
        toolchain_provider="huawei",
        toolchain_mode="mock",
        toolchain_workspace="legacy-huawei-space",
        toolchain_polling_enabled=False,
        applications_runtime="agent_dev",
        auth_mode="disabled",
        toolchain_provider_connections={
            "applications": {
                "aliyun": {
                    "toolchain_provider": "aliyun",
                    "toolchain_mode": "mock",
                    "toolchain_workspace": "aliyun-space",
                }
            },
        },
    )
    app = create_app(settings, resources_factory=lambda _: ApplicationResources())
    with TestClient(app):
        runtime = app.state.application_service.provider
        assert runtime.settings.toolchain_provider == "aliyun"
        assert runtime.settings.toolchain_workspace == "aliyun-space"
        assert (
            app.state.huawei_toolchain_clients["applications"].settings.toolchain_provider
            == "huawei"
        )
