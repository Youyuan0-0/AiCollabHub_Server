"""模型库接入合同使用本地 HTTP 替身，不访问供应方或测试数据库。"""

from contextlib import closing

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.modules.common.toolchain.client import PopClient
from app.modules.models.toolchain import StudioModelProvider


def test_disabled_model_toolchain_does_not_expose_simulated_results():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.get("/api/v1/model-toolchain")
        assert response.status_code == 503
        defaults = client.get("/api/v1/model-publication-defaults").json()
        assert defaults["toolchainAvailable"] is False


def test_studio_catalog_versions_and_import_detail_use_model_pop_contract():
    requests = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/model/list":
            data = [
                {
                    "modelId": "studio-1",
                    "modelName": "测试模型",
                    "modelType": 1,
                    "latestVersion": "v2",
                }
            ]
        elif request.url.path == "/model/list-selection":
            data = [
                {"modelId": "studio-1", "modelVersion": "v2", "frameWork": 2},
                {"modelId": "studio-1", "modelVersion": "v1", "frameWork": 1},
            ]
        elif request.url.path == "/model/get":
            data = {"modelId": "studio-1", "modelName": "测试模型"}
        else:
            raise AssertionError(f"Unexpected path: {request.url.path}")
        return httpx.Response(200, json={"success": True, "data": data})

    settings = Settings(
        _env_file=None,
        toolchain_mode="live",
        toolchain_pop_url="https://studio.invalid",
        toolchain_region="cn-test",
        toolchain_access_key_id=SecretStr("studio-ak"),
        toolchain_access_key_secret=SecretStr("studio-sk"),
    )
    with closing(
        PopClient(
            settings,
            httpx.MockTransport(upstream),
            api_version="2024-06-24",
            pop_code="aipaas",
        )
    ) as client:
        provider = StudioModelProvider(client)
        listed = provider.list_models()
        assert listed[0]["versions"] == ["v2", "v1"]
        draft = provider.get_model("studio-1", "v1")
        assert draft["version"] == "v1"
        assert draft["framework"] == "TensorFlow"
        assert draft["source"]["externalId"] == "studio-1"
        assert draft["source"]["region"] == "cn-test"
        with pytest.raises(HTTPException) as failure:
            provider.get_model("studio-1", "v0")
        assert failure.value.status_code == 409
    assert requests
    assert all(request.headers["x-acs-popcode"] == "aipaas" for request in requests)
    assert all("x-sfm-workspacecode" not in request.headers for request in requests)
    assert requests[0].headers["authorization"].startswith("acs studio-ak:")
