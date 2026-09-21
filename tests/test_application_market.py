"""使用隔离 HTTP 合同验证外部发布，不连接供应方或项目数据库。"""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.modules.applications.demo_store import create_demo_application_service
from app.modules.applications.market_contract import MarketMutation, MarketRecovery
from app.modules.applications.market_workflows import MarketWorkflows
from app.modules.common.identity import ACTORS, DEFAULT_ADMIN, DEFAULT_USER


class Supplier:
    def __init__(self):
        self.calls = []
        self.listing = {
            "listingId": "listing-1",
            "resourceId": "resource-1",
            "defaultVersionId": "version-1",
            "title": "外部应用",
            "summary": "摘要",
            "description": "说明",
            "visibility": "PRIVATE",
            "publicationStatus": "DRAFT",
        }
        self.review = None
        self.failure = None
        self.publish_delayed = False

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path.removeprefix("/himarket/v1/open-api/v1")
        if self.failure:
            result = self.failure(request)
            if result is not None:
                return result
        if path == "/application/catalog/search":
            data = {
                "content": [
                    {
                        "catalog": {**self.listing, "secret": "internal-secret"},
                        "resolved": {"apiKey": "never-return"},
                    }
                ],
                "pageNumber": 2,
                "pageSize": 12,
                "totalCount": 25,
            }
        elif path == "/catalog/categories":
            data = {
                "content": [{"categoryId": "cat-1", "name": "办公", "tenantId": "hidden"}],
                "pageNumber": 1,
                "pageSize": 20,
                "totalCount": 1,
            }
        elif path == "/catalog/listings/listing-1" and request.method == "GET":
            data = self.listing
        elif path == "/me/resources/resource-1/versions":
            data = {
                "content": [
                    {
                        "resourceId": "resource-1",
                        "versionId": "version-1",
                        "version": "v1.0",
                        "status": "RELEASED",
                    }
                ],
                "pageNumber": 1,
                "pageSize": 20,
                "totalCount": 1,
            }
        elif path == "/me/resources/resource-1/versions/version-1":
            data = {
                "version": {
                    "resourceId": "resource-1",
                    "versionId": "version-1",
                    "version": "v1.0",
                    "status": "RELEASED",
                }
            }
        elif path == "/me/listings":
            self.listing.update(json.loads(request.content))
            data = self.listing
        elif path == "/me/listings/listing-1/submit-review":
            self.listing["publicationStatus"] = "IN_REVIEW"
            self.review = {
                "submissionId": "submission-1",
                "targetId": "listing-1",
                "status": "PENDING",
            }
            data = self.review
        elif path == "/catalog/reviews/submission-1" and request.method == "GET":
            data = self.review
        elif path == "/catalog/reviews/submission-1/decision":
            self.review["status"] = json.loads(request.content)["status"]
            data = self.review
        elif path == "/catalog/listings/listing-1/publish":
            if not self.publish_delayed:
                self.listing["publicationStatus"] = "PUBLISHED"
            data = {**self.listing, "publicationStatus": "PUBLISHED"}
        else:
            return httpx.Response(404)
        return httpx.Response(
            200, json={"code": "SUCCESS", "data": deepcopy(data), "requestId": "req-1"}
        )


@pytest.fixture
def setup_market(tmp_path, monkeypatch):
    supplier = Supplier()
    service = create_demo_application_service()
    app_id = service.resources()[0]["id"]
    with service.repository.transaction() as session:
        resource = session.get_resource(app_id)
        resource.update(createdBy=DEFAULT_USER.id, organizationId=DEFAULT_USER.organization_id)
        session.put_resource(resource)
    config = {
        "profile": "test-market",
        "developer_token_env": {DEFAULT_USER.id: "TEST_MARKET_JWT"},
        "bindings": {
            app_id: {
                "owner_subject": DEFAULT_USER.id,
                "organization_id": DEFAULT_USER.organization_id,
                "resource_id": "resource-1",
                "listing_id": "listing-1",
            }
        },
        "decision_status": {"approved": "VERIFIED_APPROVAL", "rejected": "VERIFIED_REJECTION"},
    }
    path = tmp_path / "market.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("TEST_MARKET_JWT", "user-test-token")
    settings = Settings(
        _env_file=None,
        applications_storage="memory",
        auth_mode="disabled",
        applications_market="himarket",
        applications_market_url="https://market.invalid",
        applications_market_admin_token=SecretStr("admin-test-token"),
        applications_market_configuration=str(path),
    )
    workflow = MarketWorkflows(service, settings, httpx.MockTransport(supplier))
    return workflow, supplier, app_id, path


def mutate(workflow, app_id, action, **extra):
    is_admin = action in ("review_decision", "publish")
    if action == "review_decision" and extra.get("decision") == "approved":
        extra.setdefault(
            "scores",
            {
                "formatCompliance": 4,
                "safetyCompliance": 4,
                "annotationCompliance": 4,
                "structuralCompleteness": 4,
                "authenticity": 4,
                "consistency": 4,
                "cleanliness": 4,
            },
        )
    payload = MarketMutation(requestId="request-" + action, action=action, **extra)
    return workflow.execute(
        app_id, DEFAULT_ADMIN if is_admin else DEFAULT_USER, payload, is_admin=is_admin
    )


def draft():
    return {
        "title": "修改后的应用",
        "summary": "摘要",
        "description": "详细说明",
        "defaultVersionId": "version-1",
        "visibility": "PRIVATE",
    }


def approved(workflow, app_id):
    assert mutate(workflow, app_id, "save_draft", draft=draft())["status"] == "succeeded"
    assert mutate(workflow, app_id, "submit_review")["status"] == "succeeded"
    return mutate(
        workflow, app_id, "review_decision", submissionId="submission-1", decision="approved"
    )


def test_full_contract_separates_approval_and_publish_and_credentials(setup_market):
    workflow, supplier, app_id, _ = setup_market
    initial = deepcopy(workflow.service.resources())
    result = approved(workflow, app_id)
    assert result["status"] == "succeeded"
    assert result["result"]["publicationStatus"] == "IN_REVIEW"
    assert not any(r.url.path.endswith("/publish") for r in supplier.calls)
    result = mutate(workflow, app_id, "publish")
    assert result["result"]["publicationStatus"] == "PUBLISHED"
    for request in supplier.calls:
        if "/me/" in request.url.path:
            assert request.headers["Authorization"] == "Bearer user-test-token"
            assert "X-HIMARKET-TOKEN" not in request.headers
        else:
            assert request.headers["X-HIMARKET-TOKEN"] == "admin-test-token"
            assert request.headers["X-HIMARKET-OPENAPI"] == "HIMARKET_OPENAPI"
            assert "Authorization" not in request.headers
    save = next(request for request in supplier.calls if request.url.path.endswith("/me/listings"))
    assert json.loads(save.content) == {**draft(), "resourceId": "resource-1"}
    assert workflow.service.resources() == initial  # 独立外部流程不伪造原演示目录状态。
    with workflow.service.repository.transaction(is_readonly=True) as session:
        text = json.dumps(session.entries("market_operations"), ensure_ascii=False)
    assert "修改后的应用" not in text and "详细说明" not in text
    assert "user-test-token" not in text


def test_catalog_pagination_and_private_fields_do_not_leak(setup_market):
    workflow, supplier, _, _ = setup_market
    result = workflow.client.catalog(workflow.client.configuration(), DEFAULT_USER, "办公", 2, 12)
    assert result["totalCount"] == 25 and result["pageNumber"] == 2
    assert result["content"][0]["title"] == "外部应用"
    assert "never-return" not in json.dumps(result)
    assert "internal-secret" not in json.dumps(result)
    assert supplier.calls[-1].url.params["page"] == "2"
    assert "subjectId" not in supplier.calls[-1].url.params


def test_repeated_request_is_not_executed_twice_and_changed_input_conflicts(setup_market):
    workflow, supplier, app_id, _ = setup_market
    first = mutate(workflow, app_id, "save_draft", draft=draft())
    size = len(supplier.calls)
    assert mutate(workflow, app_id, "save_draft", draft=draft()) == first
    assert len(supplier.calls) == size
    with pytest.raises(HTTPException) as error:
        mutate(workflow, app_id, "save_draft", draft={**draft(), "title": "不同内容"})
    assert error.value.status_code == 409


def test_accepted_publish_must_be_queried_and_recovery_never_reposts(setup_market):
    workflow, supplier, app_id, _ = setup_market
    approved(workflow, app_id)
    supplier.publish_delayed = True
    result = mutate(workflow, app_id, "publish")
    assert result["status"] == "unknown"
    with pytest.raises(HTTPException) as error:
        workflow.execute(
            app_id,
            DEFAULT_ADMIN,
            MarketMutation(requestId="another-request", action="publish"),
            is_admin=True,
        )
    assert error.value.status_code == 409
    supplier.listing["publicationStatus"] = "PUBLISHED"
    completed = workflow.reconcile(app_id, result["id"], DEFAULT_ADMIN, is_admin=True)
    assert completed["status"] == "succeeded"
    assert sum(r.method == "POST" and r.url.path.endswith("/publish") for r in supplier.calls) == 1


@pytest.mark.parametrize(
    "case", ["timeout", "server_error", "invalid_json", "missing_data", "wrong_id"]
)
def test_uncertain_writes_stay_blocked_until_checked(setup_market, case):
    workflow, supplier, app_id, _ = setup_market

    def failure(request):
        if request.method != "POST":
            return None
        if case == "timeout":
            raise httpx.ReadTimeout("private endpoint", request=request)
        if case == "server_error":
            return httpx.Response(503, text="private error")
        if case == "invalid_json":
            return httpx.Response(200, text="oops")
        if case == "missing_data":
            return httpx.Response(200, json={"code": "SUCCESS"})
        return httpx.Response(
            200, json={"code": "SUCCESS", "data": {"listingId": "another", "resourceId": "wrong"}}
        )

    supplier.failure = failure
    result = mutate(workflow, app_id, "save_draft", draft=draft())
    assert result["status"] == "unknown"
    assert "private" not in result["message"]
    size = len(supplier.calls)
    assert mutate(workflow, app_id, "save_draft", draft=draft()) == result
    assert len(supplier.calls) == size


@pytest.mark.parametrize("case", ["business_error", "redirect", "denied"])
def test_definite_rejection_is_not_success_and_no_redirect(setup_market, case):
    workflow, supplier, app_id, _ = setup_market

    def failure(request):
        if request.method != "POST":
            return None
        if case == "business_error":
            return httpx.Response(
                200, json={"code": "DENIED", "message": "private upstream detail"}
            )
        return httpx.Response(
            302 if case == "redirect" else 403, headers={"Location": "https://other.invalid"}
        )

    supplier.failure = failure
    result = mutate(workflow, app_id, "save_draft", draft=draft())
    assert result["status"] == "failed"
    assert all(r.url.host == "market.invalid" for r in supplier.calls)


def test_permissions_binding_review_enum_and_unsupported_fields(setup_market):
    workflow, supplier, app_id, _ = setup_market
    assert workflow.status(ACTORS[2])["applications"] == []
    for actor, is_admin in ((ACTORS[2], False), (ACTORS[1], False)):
        with pytest.raises(HTTPException) as error:
            workflow.detail(app_id, actor, is_admin=is_admin)
        assert error.value.status_code == 404
    mutate(workflow, app_id, "submit_review")
    size = len(supplier.calls)
    with pytest.raises(HTTPException) as error:
        mutate(
            workflow,
            app_id,
            "review_decision",
            decision="needs_changes",
            submissionId="submission-1",
            comment="补充",
        )
    assert error.value.status_code == 503
    assert len(supplier.calls) == size
    with pytest.raises(ValueError):
        MarketMutation(
            requestId="request-invalid", action="save_draft", draft={**draft(), "inputSchema": "{}"}
        )


def test_concurrent_clicks_reserve_before_network(setup_market):
    workflow, supplier, app_id, _ = setup_market
    entered, release = Event(), Event()

    def failure(request):
        if request.method == "POST":
            entered.set()
            assert release.wait(5)
        return None

    supplier.failure = failure
    with ThreadPoolExecutor() as pool:
        first = pool.submit(mutate, workflow, app_id, "save_draft", draft=draft())
        assert entered.wait(5)
        try:
            second = mutate(workflow, app_id, "save_draft", draft=draft())
            assert second["status"] == "running"
        finally:
            release.set()
        assert first.result()["status"] == "succeeded"
    assert sum(r.method == "POST" for r in supplier.calls) == 1


def test_routes_are_wired_and_do_not_call_supplier_when_disabled(setup_market):
    workflow, supplier, app_id, _ = setup_market
    application = create_app(workflow.settings, applications_factory=lambda: workflow.service)
    application.state.application_market = workflow
    with TestClient(application) as client:
        response = client.get("/api/v1/application-market")
        assert response.status_code == 200 and response.json()["enabled"] is True
        assert (
            client.get(f"/api/v1/application-market/applications/{app_id}/versions").status_code
            == 200
        )
        assert client.get("/api/v1/application-market/catalog?page=0").status_code == 422
        assert client.get("/api/v1/admin/application-market/categories").status_code == 200
        result = client.post(
            f"/api/v1/application-market/applications/{app_id}/operations",
            json={"requestId": "http-draft-request", "action": "save_draft", "draft": draft()},
        )
        assert result.status_code == 200 and result.json()["status"] == "succeeded"
        assert (
            client.post(
                f"/api/v1/application-market/applications/{app_id}/operations",
                json={"requestId": "http-publish-request", "action": "publish"},
            ).status_code
            == 403
        )
        workflow.settings.applications_market = "disabled"
        size = len(supplier.calls)
        assert client.get("/api/v1/application-market").json()["enabled"] is False
        assert client.get("/api/v1/application-market/catalog").status_code == 503
        assert len(supplier.calls) == size


def test_approval_requires_local_quality_and_current_submission_content(setup_market):
    workflow, supplier, app_id, _ = setup_market
    mutate(workflow, app_id, "submit_review")
    with pytest.raises(HTTPException) as error:
        workflow.execute(
            app_id,
            DEFAULT_ADMIN,
            MarketMutation(
                requestId="missing-quality-score",
                action="review_decision",
                submissionId="submission-1",
                decision="approved",
            ),
            is_admin=True,
        )
    assert error.value.status_code == 422
    supplier.listing["title"] = "外部后来修改的资料"
    result = mutate(
        workflow, app_id, "review_decision", submissionId="submission-1", decision="approved"
    )
    assert result["status"] == "failed"
    assert not any(request.url.path.endswith("/decision") for request in supplier.calls)


def test_changed_version_cannot_reuse_previous_approval(setup_market):
    workflow, supplier, app_id, _ = setup_market
    approved(workflow, app_id)
    supplier.listing["defaultVersionId"] = "version-2"
    result = mutate(workflow, app_id, "publish")
    assert result["status"] == "failed"
    assert not any(request.url.path.endswith("/publish") for request in supplier.calls)


def test_external_channel_keeps_security_rules(setup_market):
    workflow, supplier, app_id, _ = setup_market
    result = mutate(workflow, app_id, "save_draft", draft={**draft(), "title": "演示拦截词"})
    assert result["status"] == "failed"
    assert not any(request.method == "POST" for request in supplier.calls)


def test_lost_post_response_can_confirm_existing_draft_without_resending(setup_market):
    workflow, supplier, app_id, _ = setup_market

    def failure(request):
        if request.method == "POST":
            supplier.listing.update(json.loads(request.content))
            raise httpx.ReadTimeout("connection lost", request=request)

    supplier.failure = failure
    result = mutate(workflow, app_id, "save_draft", draft=draft())
    assert result["status"] == "unknown"
    supplier.failure = None
    restored = MarketWorkflows(workflow.service, workflow.settings, httpx.MockTransport(supplier))
    assert restored.reconcile(app_id, result["id"], DEFAULT_USER)["status"] == "succeeded"
    assert sum(request.method == "POST" for request in supplier.calls) == 1


def test_post_with_wrong_field_type_stays_unknown(setup_market):
    workflow, supplier, app_id, _ = setup_market
    supplier.failure = lambda request: (
        httpx.Response(
            200,
            json={
                "code": "SUCCESS",
                "data": {"listingId": {"private": "secret"}, "resourceId": "resource-1"},
            },
        )
        if request.method == "POST"
        else None
    )
    result = mutate(workflow, app_id, "save_draft", draft=draft())
    assert result["status"] == "unknown"
    assert "secret" not in json.dumps(result)


def test_when_supplier_is_down_local_receipts_are_still_visible(setup_market):
    workflow, supplier, app_id, _ = setup_market
    mutate(workflow, app_id, "save_draft", draft=draft())
    supplier.failure = lambda _request: httpx.Response(503)
    detail = workflow.detail(app_id, DEFAULT_USER)
    assert detail["listing"] is None and detail["readError"]
    assert len(detail["operations"]) == 1


def test_developer_secret_from_settings_is_never_returned(setup_market, monkeypatch):
    workflow, _, _, _ = setup_market
    monkeypatch.delenv("TEST_MARKET_JWT")
    workflow.settings.applications_market_developer_tokens = {
        "TEST_MARKET_JWT": SecretStr("dotenv-secret")
    }
    assert workflow.client.headers(workflow.client.configuration(), subject=DEFAULT_USER.id) == {
        "Authorization": "Bearer dotenv-secret"
    }
    assert "dotenv-secret" not in repr(workflow.settings)


def test_lost_submit_receipt_can_be_attached_only_to_matching_recent_application(setup_market):
    from datetime import UTC, datetime

    workflow, supplier, app_id, _ = setup_market

    def lost_reply(request):
        if request.method == "POST":
            supplier.review = {
                "submissionId": "submission-1",
                "targetId": "listing-1",
                "status": "PENDING",
                "submittedAt": datetime.now(UTC).isoformat(),
            }
            supplier.listing["publicationStatus"] = "IN_REVIEW"
            raise httpx.ReadTimeout("lost reply", request=request)

    supplier.failure = lost_reply
    operation = mutate(workflow, app_id, "submit_review")
    assert operation["status"] == "unknown"
    supplier.failure = None
    supplier.review["targetId"] = "other-listing"
    recovery = MarketRecovery(externalSubmissionId="submission-1")
    assert (
        workflow.reconcile(app_id, operation["id"], DEFAULT_USER, recovery=recovery)["status"]
        == "unknown"
    )
    supplier.review["targetId"] = "listing-1"
    assert (
        workflow.reconcile(app_id, operation["id"], DEFAULT_USER, recovery=recovery)["status"]
        == "succeeded"
    )
    assert sum(request.method == "POST" for request in supplier.calls) == 1


def test_example_env_can_start_with_external_services_disabled():
    from pathlib import Path

    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env.example")
    assert settings.applications_market == "disabled"
    assert settings.applications_market_url is None
    assert settings.applications_agent_url is None
