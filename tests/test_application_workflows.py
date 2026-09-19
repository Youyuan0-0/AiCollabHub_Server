from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

API = "/api/v1"
SCORES = {
    "formatCompliance": 4,
    "safetyCompliance": 4,
    "annotationCompliance": 4,
    "structuralCompleteness": 4,
    "authenticity": 4,
    "consistency": 4,
    "cleanliness": 4,
}


@pytest.fixture
def client():
    with TestClient(create_app(Settings(app_env="test", auth_mode="disabled"))) as client:
        yield client


def select(client, actor, audience="user"):
    response = client.post(
        API + "/application-demo-session", json={"actorId": actor, "audience": audience}
    )
    assert response.status_code == 200, response.text


def submit(client, **data):
    response = client.post(
        API + "/application-publications",
        json={
            "submit": True,
            "data": {
                "name": "流程验证",
                "owner": "研发单位",
                "summary": "验证场景应用",
                "triggerType": "手动触发",
                "communicationProtocol": "HTTP",
                "deployment": "工具链托管",
                **data,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(client, record):
    response = client.post(
        f"{API}/admin/application-publications/{record['id']}/review",
        json={
            "decision": "approved",
            "revision": record["revision"],
            "scores": SCORES,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def app_id(client):
    return approve(client, submit(client))["applicationId"]


def policy(client, **changes):
    current = client.get(API + "/admin/application-policy").json()
    response = client.put(API + "/admin/application-policy", json={**current, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def sync(client, version="v1.0", **changes):
    response = client.post(
        API + "/application-toolchain/syncs",
        json={
            "externalId": "tc-report",
            "externalVersion": version,
            "requestId": str(uuid4()),
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_quality_scoring_security_and_history(client):
    record = submit(client)
    path = f"{API}/admin/application-publications/{record['id']}"
    report = client.get(path + "/quality").json()
    assert all(check["passed"] for check in report["checks"])
    assert "minimumScore" not in report and report["suggestions"]
    for scores in (None, {**SCORES, "safetyCompliance": 0}, {**SCORES, "cleanliness": True}):
        response = client.post(
            path + "/review", json={"decision": "approved", "revision": 1, "scores": scores}
        )
        assert response.status_code == 422
    assert client.get(path).json()["status"] == "pending"
    policy(client, sensitiveTerms=["流程验证"])
    assert not client.get(path + "/quality").json()["checks"][-1]["passed"]
    assert (
        client.post(
            path + "/review", json={"decision": "approved", "revision": 1, "scores": SCORES}
        ).status_code
        == 422
    )
    policy(client, sensitiveTerms=[])
    approved = approve(client, record)
    history = client.get(
        API + "/admin/application-review-history",
        params={"reviewer": "demo-admin", "decision": "approved"},
    ).json()
    assert history["total"] == 1 and history["items"][0]["scores"] == SCORES
    assert history["items"][0]["reviewerName"] == "系统管理员"
    assert (
        client.get(API + "/admin/application-review-history?reviewer=nobody").json()["total"] == 0
    )
    assert approved["reviews"][0]["data"] == record["data"]
    for url in ("/admin/application-review-history", "/admin/application-review-queue"):
        assert client.get(API + url + "?start=2026-09-15&end=2026-09-01").status_code == 422


@pytest.mark.parametrize("decision", ["approved", "rejected", "needs_changes"])
def test_seven_scores_allow_low_scores_and_ignore_legacy_threshold(client, decision):
    """旧配置不再形成门槛，三类结论都保留完整七项分数及用户评分独立性。"""
    assert "minimumQualityScore" not in policy(client, minimumQualityScore=5)
    record = submit(client)
    scores = dict.fromkeys(SCORES, 1)
    response = client.post(
        f"{API}/admin/application-publications/{record['id']}/review",
        json={"revision": 1, "decision": decision, "reason": "人工审核结论", "scores": scores},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reviews"][-1]["scores"] == scores
    if decision == "approved":
        resource = client.get(f"{API}/applications/{record['applicationId']}").json()
        assert resource["rating"] is None


@pytest.mark.parametrize(
    "scores",
    [
        {"completeness": 4, "usability": 4, "security": 4},
        {key: value for key, value in SCORES.items() if key != "cleanliness"},
        {**SCORES, "cleanliness": 1.5},
        {**SCORES, "cleanliness": 6},
    ],
)
def test_review_rejects_incomplete_or_invalid_seven_scores(client, scores):
    """完整七项与合法整数仍由服务端校验，禁止旧三项或异常分值绕过。"""
    record = submit(client)
    response = client.post(
        f"{API}/admin/application-publications/{record['id']}/review",
        json={"revision": 1, "decision": "approved", "scores": scores},
    )
    assert response.status_code == 422
    assert (
        client.get(f"{API}/admin/application-publications/{record['id']}").json()["status"]
        == "pending"
    )


@pytest.mark.parametrize(
    "changes", [{"securityLevel": "confidential"}, {"data": {"name": "演示拦截词"}}]
)
def test_submission_blocks_classification_and_sensitive_terms(client, changes):
    payload = {"submit": True, "data": {"name": "应用", "owner": "研发", "summary": "发布校验"}}
    changes = dict(changes)
    payload["data"].update(changes.pop("data", {}))
    response = client.post(API + "/application-publications", json={**payload, **changes})
    assert response.status_code == 422
    assert client.get(API + "/application-publications").json() == []


@pytest.mark.parametrize(
    "scope,targets",
    [
        ("organizations", ["org-research"]),
        ("members", ["demo-reader"]),
        ("projects", ["project-a"]),
    ],
)
def test_scope_is_enforced_on_catalog_detail_and_every_operation(client, app_id, scope, targets):
    response = client.put(
        f"{API}/admin/applications/{app_id}/visibility", json={"scope": scope, "targets": targets}
    )
    assert response.status_code == 200
    select(client, "demo-outsider")
    assert client.get(f"{API}/applications/{app_id}").status_code == 404
    assert client.get(API + "/applications", params={"q": "流程验证"}).json()["total"] == 0
    assert client.get(f"{API}/applications/{app_id}/ratings").status_code == 404
    assert (
        client.put(f"{API}/applications/{app_id}/interaction", json={"favorite": True}).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/applications/{app_id}/downloads", json={"requestId": str(uuid4())}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{API}/applications/{app_id}/runs",
            json={"requestId": str(uuid4()), "input": {"question": "test"}},
        ).status_code
        == 404
    )
    select(client, "demo-reader")
    assert client.get(f"{API}/applications/{app_id}").status_code == 200
    assert client.get(f"{API}/applications/{app_id}").json()["canPublishVersion"] is False
    assert client.post(f"{API}/applications/{app_id}/version-publications").status_code == 403
    select(client, "demo-reader", "admin")
    assert client.get(API + "/admin/applications").status_code == 403


def test_publications_and_organization_management_are_isolated(client, app_id):
    select(client, "demo-reader")
    assert client.get(API + "/application-publications").json() == []
    assert client.post(API + "/application-publications", json={"data": {}}).status_code == 403
    select(client, "demo-org-admin", "admin")
    assert client.get(f"{API}/admin/applications/{app_id}").status_code == 200
    assert client.put(API + "/admin/application-policy", json={"revision": 1}).status_code == 403
    select(client, "demo-admin", "user")
    version = client.post(f"{API}/applications/{app_id}/version-publications").json()
    assert version["createdBy"] == "demo-publisher" and version["organizationId"] == "org-research"


def test_favorites_ratings_and_downloads_have_defined_counts(client, app_id):
    path = f"{API}/applications/{app_id}"
    for _ in range(2):
        value = client.put(path + "/interaction", json={"favorite": True, "rating": 5}).json()
        assert value["favoriteCount"] == 1 and value["ratingCount"] == 1
    select(client, "demo-reader")
    second = client.put(path + "/interaction", json={"rating": 3, "comment": "需优化"}).json()
    assert second["ratingAverage"] == 4 and second["ratingCount"] == 2
    third = client.put(path + "/interaction", json={"rating": 1}).json()
    assert third["ratingAverage"] == 3 and third["ratingCount"] == 2
    assert client.put(path + "/interaction", json={"rating": True}).status_code == 422
    key = str(uuid4())
    one = client.post(path + "/downloads", json={"requestId": key}).json()
    assert client.post(path + "/downloads", json={"requestId": key}).json() == one
    assert one["content"]["contact"] == ""
    assert client.get(path).json()["downloadCount"] == 1
    select(client, "demo-publisher")
    client.put(path + "/interaction", json={"favorite": False})
    assert client.get(path).json()["favoriteCount"] == 0


def test_subscription_version_notification_and_personal_read_state(client, app_id):
    select(client, "demo-reader")
    client.put(f"{API}/applications/{app_id}/interaction", json={"subscribed": True})
    assert len(client.get(API + "/application-activity").json()["subscriptions"]) == 1
    select(client, "demo-publisher")
    draft = client.post(f"{API}/applications/{app_id}/version-publications").json()
    record = client.put(
        f"{API}/application-publications/{draft['id']}",
        json={
            "data": {**draft["data"], "version": "v2.0"},
            "submit": True,
            "revision": draft["revision"],
        },
    ).json()
    approve(client, record)
    select(client, "demo-reader")
    activity = client.get(API + "/application-activity").json()
    notice = activity["notifications"][0]
    assert notice["kind"] == "new_version" and "v2.0" in notice["title"]
    assert client.put(f"{API}/application-notifications/{notice['id']}/read").json()["read"] is True
    select(client, "demo-outsider")
    assert client.put(f"{API}/application-notifications/{notice['id']}/read").status_code == 404


def test_runs_validate_input_are_idempotent_and_measure_failures(client):
    app_id = approve(
        client,
        submit(
            client,
            inputSchema='{"type":"object","required":["question"],"properties":{"question":{"type":"string"}}}',
        ),
    )["applicationId"]
    path = f"{API}/applications/{app_id}/runs"
    payload = {"requestId": str(uuid4()), "input": {"question": "整理报告"}}
    assert client.post(path, json={**payload, "input": {"question": 2}}).status_code == 422
    assert client.post(path, json={**payload, "options": {"unknown": "value"}}).status_code == 422
    result = client.post(path, json=payload).json()
    assert result["status"] == "succeeded" and result["mode"] == "simulation"
    assert "fingerprint" not in result
    assert client.post(path, json=payload).json() == result
    assert client.post(path, json={**payload, "input": {"question": "changed"}}).status_code == 409
    policy(client, failureThreshold=1)
    failure = client.post(
        path, json={**payload, "requestId": str(uuid4()), "simulateFailure": True}
    ).json()
    assert failure["status"] == "failed"
    resource = client.get(f"{API}/admin/applications/{app_id}").json()
    assert resource["usage"] == 1 and resource["runtime"]["sampleCount"] == 2
    assert resource["runtime"]["successRate"] == 0.5
    assert len(client.get(API + "/admin/application-notifications").json()) == 1


def test_schema_remote_references_are_not_fetched(client, monkeypatch):
    import urllib.request

    def unexpected(*args, **kwargs):
        raise AssertionError("remote JSON Schema must not be loaded")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    app_id = approve(
        client, submit(client, inputSchema='{"$ref":"https://example.invalid/schema.json"}')
    )["applicationId"]
    result = client.post(
        f"{API}/applications/{app_id}/runs", json={"requestId": str(uuid4()), "input": {"x": 1}}
    )
    assert result.status_code == 422 and "外部引用" in result.json()["detail"]


def test_runtime_notification_filter_excludes_review_messages(client):
    select(client, "demo-admin")
    app_id = approve(client, submit(client))["applicationId"]
    policy(client, failureThreshold=1)
    result = client.post(
        f"{API}/applications/{app_id}/runs",
        json={
            "requestId": str(uuid4()),
            "input": {"question": "核查通知"},
            "simulateFailure": True,
        },
    )
    assert result.json()["status"] == "failed"
    path = API + "/admin/application-notifications"
    assert {item["kind"] for item in client.get(path).json()} == {"review", "run_failure"}
    notifications = client.get(path, params={"kind": "run_failure"}).json()
    assert len(notifications) == 1 and notifications[0]["kind"] == "run_failure"
    assert client.get(path, params={"kind": "invalid"}).status_code == 422
    select(client, "demo-reader", audience="admin")
    assert client.get(path, params={"kind": "run_failure"}).status_code == 403


def test_legacy_rating_setting_maps_to_comprehensive_sort_and_handles_ties(client):
    from app.modules.common.catalog import query_resources
    from app.modules.common.schemas import CatalogQuery

    saved = policy(client, defaultSort="rating")
    assert saved["defaultSort"] == "recommended"
    assert client.get(API + "/application-presentation").json()["defaultSort"] == "recommended"
    base = {"name": "核查", "description": "", "owner": "", "category": "", "tags": []}
    items = [
        {**base, "id": key, "rating": rating}
        for key, rating in [("none", None), ("b", 5), ("low", 2), ("a", 5)]
    ]
    result = query_resources(items, CatalogQuery(sort="rating", page_size=2))
    assert [item["id"] for item in result["items"]] == ["a", "b"]
    next_page = query_resources(items, CatalogQuery(sort="rating", page_size=2, page=2))
    assert [item["id"] for item in next_page["items"]] == ["low", "none"]


@pytest.mark.parametrize("stored_sort", ["recommended", "rating"])
def test_comprehensive_sort_reads_legacy_policy_without_rewriting_it(client, stored_sort):
    current = client.get(API + "/admin/application-policy").json()
    assert current["defaultSort"] == "recommended"
    repository = client.app.state.application_service.repository
    with repository.transaction() as session:
        session.put_entry("policy", "current", {**current, "defaultSort": stored_sort})

    for path in ("/admin/application-policy", "/application-presentation"):
        received = client.get(API + path).json()
        assert received["defaultSort"] == "recommended"
        assert received["revision"] == current["revision"]
    with repository.transaction(is_readonly=True) as session:
        assert session.entry("policy", "current")["defaultSort"] == stored_sort

    saved = policy(client, defaultSort="recommended")
    assert saved["defaultSort"] == "recommended"
    assert saved["revision"] == current["revision"] + 1
    assert policy(client, defaultSort="downloads")["defaultSort"] == "downloads"


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (None, ["a", "b", "popular", "curated", "unrated"]),
        ("recommended", ["a", "b", "popular", "curated", "unrated"]),
        ("rating", ["a", "b", "popular", "curated", "unrated"]),
    ],
)
def test_comprehensive_sort_uses_rating_ignores_recommendation_and_has_stable_pages(
    client, monkeypatch, sort, expected
):
    base = {"name": "排序核查", "description": "", "owner": "", "category": "", "tags": []}
    items = [
        {**base, "id": "popular", "rating": 2, "badge": "热门", "usage": 1000},
        {**base, "id": "unrated", "rating": None, "badge": "新上线", "usage": 100},
        {**base, "id": "b", "rating": 5, "badge": "", "usage": 200},
        {**base, "id": "a", "rating": 5, "badge": "", "usage": 200},
        {**base, "id": "curated", "rating": 1, "badge": "", "usage": 1},
    ]
    monkeypatch.setattr(
        client.app.state.application_service,
        "resources",
        lambda *args, **kwargs: [dict(item) for item in items],
    )
    monkeypatch.setattr(
        client.app.state.application_service.workflows,
        "policy",
        lambda: {"recommendedIds": ["curated"], "revision": 1},
    )
    params = {"pageSize": 2, **({"sort": sort} if sort else {})}
    received = []
    for page in (1, 2, 3):
        response = client.get(API + "/applications", params={**params, "page": page})
        assert response.status_code == 200
        received.extend(item["id"] for item in response.json()["items"])
    assert received == expected


def test_concurrent_repeated_run_invokes_provider_once(client, app_id, monkeypatch):
    provider = client.app.state.application_service.provider
    original, calls = provider.invoke, []

    def invoke(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(provider, "invoke", invoke)
    payload = {"requestId": str(uuid4()), "input": {"question": "并发体验"}}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: client.post(f"{API}/applications/{app_id}/runs", json=payload), range(2)
            )
        )
    assert all(result.status_code == 200 for result in results) and len(calls) == 1
    assert client.get(f"{API}/applications/{app_id}").json()["usage"] == 1


def test_toolchain_duplicate_versions_failures_and_new_version_review(client):
    failed = sync(client, simulateFailure=True)
    assert failed["status"] == "failed" and not failed["publicationId"]
    first = sync(client)
    assert first["status"] == "succeeded" and first["mapping"]
    assert sync(client)["publicationId"] == first["publicationId"]
    assert sync(client, "v2.0")["status"] == "failed"
    draft = client.get(f"{API}/application-publications/{first['publicationId']}").json()
    record = client.put(
        f"{API}/application-publications/{draft['id']}",
        json={"data": draft["data"], "submit": True, "revision": draft["revision"]},
    ).json()
    approve(client, record)
    next_sync = sync(client, "v2.0")
    version = client.get(f"{API}/application-publications/{next_sync['publicationId']}").json()
    assert version["applicationId"] == record["applicationId"]
    assert version["source"]["externalVersion"] == "v2.0"
    assert client.get(f"{API}/applications/{record['applicationId']}").json()["version"] == "v1.0"
    assert len(client.get(API + "/admin/application-syncs").json()) == 5


def test_policy_presentation_revision_and_scope(client, app_id):
    old = client.get(API + "/admin/application-policy").json()
    policy(
        client,
        recommendedIds=[app_id],
        hotCategories=["文档创作"],
        pageSize=6,
        announcement="新应用已发布",
        toolchainUrl="https://example.com/workspace",
    )
    assert client.put(API + "/admin/application-policy", json=old).status_code == 409
    presentation = client.get(API + "/application-presentation").json()
    assert presentation["pageSize"] == 6 and presentation["toolchainUrl"].startswith("https://")
    assert "sensitiveTerms" not in presentation
    items = client.get(API + "/applications?q=流程验证").json()["items"]
    assert items[0]["badge"] == "推荐"
    assert (
        client.put(
            API + "/admin/application-policy",
            json={**old, "notificationTemplate": "{name.__class__}"},
        ).status_code
        == 422
    )


def test_toolchain_development_enters_importable_catalog(client):
    response = client.post(
        API + "/application-toolchain/development-publications",
        json={
            "data": {
                "name": "自建演练应用",
                "owner": "开发者",
                "summary": "模拟工具链开发",
                "triggerType": "手动触发",
                "communicationProtocol": "HTTP",
                "deployment": "工具链托管",
            }
        },
    )
    assert response.status_code == 200, response.text
    source = response.json()
    assert any(
        item["externalId"] == source["externalId"]
        for item in client.get(API + "/application-toolchain/resources").json()
    )
    result = sync(
        client, externalId=source["externalId"], externalVersion=source["externalVersion"]
    )
    assert result["status"] == "succeeded"
    select(client, "demo-reader")
    assert (
        client.post(
            API + "/application-toolchain/development-publications", json={"data": {}}
        ).status_code
        == 403
    )


def test_expired_demo_identity_can_be_selected_again_without_granting_access(client):
    from app.modules.applications.identity import DemoIdentityProvider

    select(client, "demo-reader")
    select(client, "demo-admin", "admin")
    client.app.state.application_identity = DemoIdentityProvider()
    for path in ("/application-session", "/admin/application-session"):
        session = client.get(API + path).json()
        assert session["current"] is None and len(session["actors"]) == 5
    assert client.get(API + "/applications").status_code == 401
    assert client.get(API + "/admin/applications").status_code == 401
    select(client, "demo-reader")
    assert client.get(API + "/applications").status_code == 200
