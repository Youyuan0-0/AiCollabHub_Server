"""测试问题梳理中的分类、密级、组织及模型搜索回归。"""

import json

import pytest
from test_dataset_publishing import approve
from test_dataset_workspace import client as client
from test_dataset_workspace import data as data
from test_resource_requirements import scores

from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.service import list_resources


@pytest.mark.parametrize(
    "value,expected",
    [("public", "public"), ("internal", "internal"), ("公开", "public"), ("内部", "internal")],
)
def test_security_level_survives_review_and_catalog(client, data, value, expected):
    data["securityLevel"] = value
    response = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True})
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["securityLevel"] == record["data"]["securityLevel"] == expected
    reviewed = approve(client, record)
    assert reviewed.status_code == 200, reviewed.text
    saved = client.get("/api/v1/dataset-publications/" + record["id"]).json()
    assert saved["data"]["securityLevel"] == expected
    assert saved["reviews"][0]["data"]["securityLevel"] == expected
    detail = client.get("/api/v1/admin/datasets/" + record["datasetId"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["securityLevel"] == expected
    assert "securityLevel" not in client.get("/api/v1/datasets/" + record["datasetId"]).json()


@pytest.mark.parametrize("value", ["秘密", "confidential", None, {}])
def test_unsupported_security_level_cannot_be_saved(client, value):
    response = client.post("/api/v1/dataset-publications", json={"data": {"securityLevel": value}})
    assert response.status_code == 422
    assert client.get("/api/v1/dataset-publications").json() == []


def test_old_taxonomy_is_normalized_on_save_and_filter(client):
    draft = client.post(
        "/api/v1/dataset-publications",
        json={
            "data": {
                "dataApplication": ["通识数据", "智能试验", "智能实验", "历史自定义"],
                "dataShape": ["时序"],
                "aiPurpose": "微调数据集",
            }
        },
    ).json()
    assert draft["data"]["dataApplication"] == ["通识数据集", "智能实验", "历史自定义"]
    assert draft["data"]["dataShape"] == ["时序数据"]
    assert draft["data"]["aiPurpose"] == "后训练"
    resources = [
        {
            "id": "legacy",
            "name": "旧分类",
            "description": "",
            "owner": "测试单位",
            "usage": 0,
            "category": "智能试验",
            "facets": {"dataApplication": ["智能试验"], "aiPurpose": ["微调数据集"]},
        }
    ]
    for application, purpose in [("智能试验", "微调数据集"), ("智能实验", "后训练")]:
        query = CatalogQuery(
            category=application,
            facets=json.dumps({"dataApplication": [application], "aiPurpose": [purpose]}),
        )
        result = list_resources(query, resources)
        assert result["total"] == 1
        assert result["items"][0]["category"] == "智能实验"
    assert resources[0]["category"] == "智能试验"
    resources[0]["facets"] = None
    assert list_resources(CatalogQuery(), resources)["items"][0]["facets"] == {}


def test_organizations_use_trusted_directory_without_changing_actor(client, tmp_path):
    directory = tmp_path / "organizations.json"
    directory.write_text(
        json.dumps({"organizations": [{"id": "manually-imported", "name": "统一录入单位"}]}),
        encoding="utf-8",
    )
    client.app.state.settings.applications_identity_directory = str(directory)
    defaults = client.get("/api/v1/dataset-publication-defaults")
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["organizations"] == [{"id": "manually-imported", "name": "统一录入单位"}]
    draft = client.post(
        "/api/v1/dataset-publications", json={"data": {"owner": "统一录入单位"}}
    ).json()
    assert draft["organizationId"] == defaults.json()["organizationId"]
    assert draft["organizationId"] != "manually-imported"


def test_model_task_framework_search_and_four_capability_filters(client):
    response = client.post(
        "/api/v1/model-publications",
        json={
            "submit": True,
            "data": {
                "name": "SearchRegressionModel",
                "summary": "搜索回归",
                "taskType": "TaskOnlyNeedle",
                "framework": "FrameworkOnlyNeedle",
                "modelFormat": "Safetensors",
                "precision": "BF16",
                "configJson": "{}",
                "license": "MIT",
                "serviceCapabilities": ["体验", "训练", "评测", "部署"],
            },
        },
    )
    assert response.status_code == 201, response.text
    record = response.json()
    reviewed = client.post(
        "/api/v1/admin/model-publications/" + record["id"] + "/review",
        json={
            "decision": "approved",
            "revision": record["revision"],
            "reason": "搜索复测通过",
            "scores": scores(),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    for keyword in ["taskonlyneedle", "frameworkonlyneedle"]:
        page = client.get("/api/v1/models", params={"q": keyword}).json()
        assert page["total"] == 1
        assert page["items"][0]["name"] == "SearchRegressionModel"
    for capability in ["体验", "训练", "评测", "部署"]:
        page = client.get(
            "/api/v1/models",
            params={"q": "SearchRegressionModel", "facets": json.dumps({"support": [capability]})},
        ).json()
        assert page["total"] == 1
