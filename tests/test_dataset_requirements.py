"""任务表缺口回归：七维审核、必填规模、公开字段隔离和审核历史检索。"""

import json

import pytest
from test_dataset_admin import SCORES
from test_dataset_workspace import client as client
from test_dataset_workspace import data as data


@pytest.mark.parametrize(
    "scores",
    [
        {"completeness": 4, "usability": 4, "security": 5},
        {key: value for key, value in SCORES.items() if key != "authenticity"},
        {**SCORES, "cleanliness": 2.5},
        {**SCORES, "formatCompliance": True},
        {**SCORES, "consistency": 6},
    ],
)
def test_new_reviews_require_seven_valid_dimensions(client, data, scores):
    """旧三维不能代替新审核，非法分值不能改变状态或产生历史。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    path = "/api/v1/admin/dataset-publications/" + record["id"]
    response = client.post(
        path + "/review",
        json={
            "decision": "approved",
            "revision": 1,
            "scores": scores,
        },
    )
    assert response.status_code == 422
    assert client.get(path).json() == record


@pytest.mark.parametrize(
    "changes,accepted",
    [
        ({"descriptionMarkdown": "  "}, False),
        ({"recordCount": None, "dataSize": "  "}, False),
        ({"recordCount": 0, "dataSize": ""}, True),
        ({"recordCount": None, "dataSize": "2 GB"}, True),
    ],
)
def test_submission_requires_description_and_one_scale(client, data, changes, accepted):
    """仅提交时要求描述与规模；空草稿仍可保存，零记录数是有效值。"""
    payload = {"data": {**data, **changes}, "submit": True}
    response = client.post("/api/v1/dataset-publications", json=payload)
    assert response.status_code == (201 if accepted else 422)
    assert client.post("/api/v1/dataset-publications", json={"data": {}}).status_code == 201


def test_audit_filters_reviewer_and_inclusive_dates(client, data):
    """审核员和 UTC 日期组合筛选，不匹配记录不得进入分页统计。"""
    record = client.post("/api/v1/dataset-publications", json={"data": data, "submit": True}).json()
    response = client.post(
        "/api/v1/admin/dataset-publications/" + record["id"] + "/review",
        json={"decision": "approved", "revision": 1, "scores": SCORES},
    )
    assert response.status_code == 200
    review = response.json()["reviews"][0]
    assert review["scores"] == SCORES
    day = review["reviewedAt"][:10]
    path = "/api/v1/admin/dataset-audit"
    for reviewer in (review["reviewerName"], review["reviewerId"]):
        page = client.get(path, params={"reviewer": reviewer, "start": day, "end": day}).json()
        assert page["total"] == 1
    for params in ({"reviewer": "不存在的审核员"}, {"end": "2000-01-01"}, {"start": "2999-01-01"}):
        assert client.get(path, params=params).json()["total"] == 0
    assert client.get(path, params={"start": "2026-02-01", "end": "2026-01-01"}).status_code == 422


def test_public_catalog_hides_internal_fields_and_supports_organization_union(client):
    """列表和详情统一隔离后台字段，组织筛选同组取并集后再分页。"""
    items = client.get("/api/v1/datasets", params={"pageSize": 100}).json()["items"]
    owners = list(dict.fromkeys(item["owner"] for item in items))[:2]
    result = client.get(
        "/api/v1/datasets",
        params={
            "pageSize": 100,
            "facets": json.dumps({"organization": owners}),
        },
    ).json()
    assert {item["id"] for item in result["items"]} == {
        item["id"] for item in items if item["owner"] in owners
    }
    for item in items:
        detail = client.get("/api/v1/datasets/" + item["id"]).json()
        for public in (item, detail):
            assert (
                not {"metadata", "securityLevel", "dataLevel", "visibility", "endpoint", "contact"}
                & public.keys()
            )
        admin = client.get("/api/v1/admin/datasets/" + item["id"]).json()
        assert "visibility" in admin and "metadata" in admin
