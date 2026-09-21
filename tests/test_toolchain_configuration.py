"""配置管理的持久化、权限、脱敏及真实装配验证；不访问数据库或供应方。"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_auth import auth as auth
from test_auth import csrf, login

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.main import create_app
from app.modules.applications.runtime import AgentDevProvider
from app.modules.common.toolchain.configuration import ToolchainConfigurationStore, module_settings
from app.modules.common.toolchain.configuration_schemas import SaveToolchainConfiguration
from app.modules.knowledge.schemas import IndexInput

PATH = "/api/v1/admin/toolchain-configuration"


def application(settings=None):
    return create_app(settings or Settings(), resources_factory=lambda _: ApplicationResources())


@pytest.fixture
def client():
    with TestClient(application()) as client:
        yield client


def payload(client):
    view = client.get(PATH).json()
    return {"revision": view["revision"], "configuration": view["configuration"]}


def test_initial_read_does_not_create_file_or_require_database(client, tmp_path):
    result = client.get(PATH)
    assert result.status_code == 200
    assert result.json()["activeModes"]["datasets"] == "disabled"
    assert result.json()["restartRequired"] is False
    assert client.app.state.database_engine is None
    assert not (tmp_path / ".runtime/toolchain-configuration.json").exists()


def test_save_is_masked_and_only_applied_on_restart(client):
    data = payload(client)
    for module in ("applications", "datasets", "knowledge"):
        data["configuration"][module]["connection"]["mode"] = "mock"
    data["credentials"] = {
        "datasetsAccessKeyId": "ak-private",
        "datasetsAccessKeySecret": "sk-private",
    }
    saved = client.put(PATH, json=data)
    assert saved.status_code == 200, saved.text
    assert "ak-private" not in saved.text and "sk-private" not in saved.text
    assert saved.json()["restartRequired"] is True
    assert client.app.state.settings.toolchain_mode == "disabled"
    stored = json.loads(client.app.state.toolchain_configuration.path.read_text("utf-8"))
    assert stored["credentials"]["datasetsAccessKeySecret"] == "sk-private"
    with TestClient(application()) as restarted:
        result = restarted.get(PATH).json()
        assert result["restartRequired"] is False
        assert result["activeModes"]["datasets"] == "mock"
        assert restarted.app.state.toolchain_client.mode == "mock"
        assert restarted.get("/api/v1/dataset-toolchain/resources").status_code == 200
        assert restarted.get("/api/v1/application-toolchain/resources").status_code == 200
        assert restarted.app.state.knowledge_workspace.engine.mode == "mock"


def test_keep_replace_and_explicit_clear_secrets(client):
    data = payload(client)
    data["credentials"] = {"appKey": "original-key", "datasetsAccessKeyId": "original-ak"}
    assert client.put(PATH, json=data).status_code == 200
    data = payload(client)
    data["credentials"] = {"appKey": "", "datasetsAccessKeyId": "updated-ak"}
    assert client.put(PATH, json=data).status_code == 200
    document = client.app.state.toolchain_configuration.read()
    assert document.credentials["appKey"].get_secret_value() == "original-key"
    assert document.credentials["datasetsAccessKeyId"].get_secret_value() == "updated-ak"
    data = payload(client)
    data["clearCredentials"] = ["appKey"]
    assert client.put(PATH, json=data).json()["credentialStatus"]["appKey"] is False
    assert "appKey" not in client.app.state.toolchain_configuration.read().credentials


@pytest.mark.parametrize("actor", ["demo-reader", "demo-publisher", "demo-org-admin"])
def test_only_system_admin_can_read_or_save(client, actor):
    data = payload(client)
    token, _ = client.app.state.application_identity.select(actor)
    client.cookies.set("application_admin", token)
    assert client.get(PATH).status_code == 403
    assert client.put(PATH, json=data).status_code == 403
    assert not client.app.state.toolchain_configuration.path.exists()


def test_stale_and_concurrent_revisions_do_not_overwrite(client):
    store = client.app.state.toolchain_configuration
    other_store = ToolchainConfigurationStore(Settings())
    data = SaveToolchainConfiguration.model_validate(payload(client))

    def save(target):
        try:
            return target.save(data, "admin")["revision"]
        except HTTPException as error:
            assert error.status_code == 409
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [store, other_store]))
    assert results.count("conflict") == 1
    assert store.read().revision in results
    assert client.put(PATH, json=data.model_dump(mode="json", by_alias=True)).status_code == 409


@pytest.mark.parametrize("case", ["live", "runtime", "url", "duplicate", "database", "secret"])
def test_invalid_configuration_preserves_file_and_hides_inputs(client, case):
    assert client.put(PATH, json=payload(client)).status_code == 200
    store = client.app.state.toolchain_configuration
    before = store.path.read_bytes()
    data = payload(client)
    if case == "live":
        data["configuration"]["datasets"]["connection"]["mode"] = "live"
    elif case == "runtime":
        data["configuration"]["applications"]["runtime"] = "agent_dev"
    elif case == "url":
        data["configuration"]["datasets"]["connection"]["url"] = (
            "https://user:never-echo@example.test"
        )
    elif case == "duplicate":
        binding = dict(resourceId="a", versionId="v", agentCode="code", agentVersion="1")
        data["configuration"]["applications"]["bindings"] = [binding, binding]
    elif case == "database":
        data["configuration"]["databaseUrl"] = "postgres://never-echo"
    else:
        data["credentials"] = {"appKey": {"password": "never-echo"}}
    result = client.put(PATH, json=data)
    assert result.status_code == 422, result.text
    assert "never-echo" not in result.text
    assert "input" not in result.json().get("detail", [])
    assert store.path.read_bytes() == before


def test_failed_replace_preserves_old_file_and_cleans_temp(client, monkeypatch):
    assert client.put(PATH, json=payload(client)).status_code == 200
    store = client.app.state.toolchain_configuration
    before = store.path.read_bytes()

    def fail(*args):
        raise OSError("internal-path-with-sensitive-data")

    monkeypatch.setattr("app.modules.common.toolchain.configuration.os.replace", fail)
    result = client.put(PATH, json=payload(client))
    assert result.status_code == 503
    assert "sensitive-data" not in result.text
    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(".toolchain-*"))


def test_saved_bindings_and_knowledge_profiles_reach_existing_providers(client):
    data = payload(client)
    data["configuration"]["datasets"]["connection"].update(
        mode="live",
        url="https://vendor.invalid",
        workspace="test-space",
        allowedSubjects=["demo-publisher"],
    )
    data["configuration"]["applications"].update(
        runtime="agent_dev",
        url="https://runtime.invalid",
        bindings=[
            dict(
                resourceId="local-app", versionId="local-v1", agentCode="agent-1", agentVersion="1"
            )
        ],
    )
    data["configuration"]["knowledge"]["indexProfiles"] = [
        {
            "confirmed": True,
            "match": dict(
                mode="高质量模式",
                embeddingModel="confirmed-model",
                chunkMode="固定长度",
                chunkSize=700,
                chunkOverlap=50,
            ),
            "pipelineOptions": {"supplierTestOption": "verified"},
        }
    ]
    data["credentials"] = {"appKey": "key"}
    for module in ("applications", "datasets", "knowledge"):
        data["configuration"][module]["connection"] = data["configuration"]["datasets"][
            "connection"
        ].copy()
        data["credentials"].update({module + "AccessKeyId": "ak", module + "AccessKeySecret": "sk"})
    assert client.put(PATH, json=data).status_code == 200
    with TestClient(application()) as restarted:
        settings = restarted.app.state.settings
        assert settings.database_url is None and settings.applications_storage == "memory"
        runtime = restarted.app.state.application_service.provider
        assert isinstance(runtime, AgentDevProvider)
        assert runtime.binding({"id": "local-app", "versionId": "local-v1"}).agent_code == "agent-1"
        engine = restarted.app.state.knowledge_workspace.engine
        assert engine.index_options(
            IndexInput(
                embedding_model="confirmed-model",
                chunk_mode="固定长度",
                chunk_size=700,
                chunk_overlap=50,
            )
        ) == {"supplierTestOption": "verified"}


def test_saved_file_takes_precedence_without_changing_environment_or_database(client, tmp_path):
    data = payload(client)
    data["configuration"]["datasets"]["connection"].update(mode="mock", workspace="saved-space")
    assert client.put(PATH, json=data).status_code == 200
    baseline = Settings(toolchain_workspace="environment-space", database_url="sqlite://")
    store = ToolchainConfigurationStore(baseline)
    configured = store.settings(baseline)
    assert module_settings(configured, "datasets").toolchain_workspace == "saved-space"
    assert module_settings(configured, "applications").toolchain_workspace == ""
    assert baseline.toolchain_workspace == "environment-space"
    assert configured.database_url == baseline.database_url
    assert not (tmp_path / ".env").exists()


def test_corrupt_file_is_reported_without_overwriting(client):
    store = client.app.state.toolchain_configuration
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"invalid":"private-secret"}', encoding="utf-8")
    assert client.get(PATH).status_code == 503
    with pytest.raises(RuntimeError, match="工具链配置读取失败") as error:
        application()
    assert "private-secret" not in str(error.value)
    assert "private-secret" in store.path.read_text("utf-8")


def test_authenticated_endpoint_reuses_admin_session_and_csrf(request):
    # 复用隔离 SQLite/FakeRedis 登录夹具，不连接本机 .env 中的远程服务。
    client, _, iam, _ = request.getfixturevalue("auth")
    assert client.get(PATH).status_code == 401
    login(client, iam, subject="mock-user")
    assert client.get(PATH).status_code == 403
    login(client, iam, subject="mock-admin", scope="admin")
    data = payload(client)
    assert client.put(PATH, json=data).status_code == 403
    saved = client.put(PATH, json=data, headers=csrf(client, "admin"))
    assert saved.status_code == 200
    assert saved.json()["updatedBy"] == "mock-admin"


def test_no_managed_file_preserves_legacy_binding_behavior(tmp_path):
    # 未启用后台文件时，不提前读取或冻结原有按次读取的应用运行绑定文件。
    legacy = tmp_path / "existing-bindings.json"
    baseline = Settings(applications_agent_bindings=str(legacy))
    store = ToolchainConfigurationStore(baseline)
    assert store.settings(baseline) is baseline
    assert baseline.applications_agent_bindings == str(legacy)
    with pytest.raises(HTTPException) as error:
        store.view()
    assert error.value.status_code == 503
    legacy.write_text("{}", encoding="utf-8")
    assert store.view()["configuration"]["applications"]["bindings"] == []
