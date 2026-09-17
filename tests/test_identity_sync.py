import json
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_auth import SECRET, begin, login
from test_auth import auth as auth

from app.core.config import Settings
from app.main import create_app
from app.modules.common.auth.models import AuthSession, OperationLog, User
from app.modules.common.identity_sync import service
from app.modules.common.identity_sync.models import IamSyncReceipt
from app.modules.common.identity_sync.schemas import SyncBatch

PATH = "/api/v1/auth/mock/identity-sync"
HEADERS = {"X-Mock-IAM-Key": SECRET}


def batch(subject="mock-user", status="active", version=100, *, role=False, action="push"):
    change = {"subject": subject, "status": status}
    if role:
        change["roleCode"] = "platform_admin"
    return {
        "appId": "platform",
        "resType": "userlinkedrole" if role else "user",
        "action": action,
        "syncTime": version,
        "data": [change],
    }


def push(client, value):
    return client.post(PATH, json=value, headers=HEADERS)


@pytest.mark.parametrize("status", ["disabled", "deleted"])
def test_user_status_revokes_both_portals_and_requires_new_login_after_reactivation(auth, status):
    a, b, iam, engine = auth
    login(a, iam, subject="mock-admin")
    b.cookies.update(a.cookies)
    value = batch("mock-admin", status)
    response = push(b, value)
    assert response.status_code == 200, response.text
    with Session(engine) as db:
        log = db.scalar(select(OperationLog).where(OperationLog.action == "sync"))
        assert log.data == {"changedUsers": 1, "revokedSessions": 1}
    for client in (a, b):
        for scope in ("user", "admin"):
            result = client.get(f"/api/v1/auth/session?scope={scope}")
            assert result.status_code == 401 and result.json()["detail"] == "account_unavailable"
        assert client.get("/api/v1/datasets").status_code == 401
    assert "account_unavailable" in login(a, iam, subject="mock-admin").headers["location"]
    assert push(b, batch("mock-admin", "active", 101)).status_code == 200
    assert a.get("/api/v1/auth/session").status_code == 401
    login(a, iam, subject="mock-admin")
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 200
    with Session(engine) as db:
        sessions = db.scalars(
            select(AuthSession).join(User).where(User.iam_subject == "mock-admin")
        ).all()
        assert sum(value.revoked_at is None for value in sessions) == 1


@pytest.mark.parametrize("subject", ["mock-admin", "mock-admin-b"])
def test_admin_revocation_overrides_mock_defaults_without_logging_out_user_portal(auth, subject):
    a, b, iam, engine = auth
    login(a, iam, subject=subject)
    b.cookies.update(a.cookies)
    revoke = batch(subject, "unauthorized", role=True)
    assert push(a, revoke).status_code == 200
    for client in (a, b):
        session = client.get("/api/v1/auth/session")
        assert session.status_code == 200 and not session.json()["user"]["canAccessAdmin"]
        assert client.get("/api/v1/admin/feedback").status_code == 403
    # 重新完成模拟登录也不能绕过已同步的撤权。
    login(a, iam, subject=subject)
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 403
    assert push(b, batch(subject, "authorized", 99, role=True)).status_code == 200
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 403
    assert push(b, batch(subject, "authorized", 101, role=True)).status_code == 200
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 200
    with Session(engine) as db:
        assert db.scalar(select(User).where(User.iam_subject == subject)).role_sync_time == 101


def test_role_and_user_versions_are_independent_and_role_push_cannot_enable_disabled_user(auth):
    a, b, iam, engine = auth
    assert push(a, batch("mock-user", "authorized", 500, role=True)).status_code == 200
    with Session(engine) as db:
        assert db.scalar(select(User)).status == "pending"
    login(a, iam)
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 200
    assert push(b, batch("mock-user", "disabled", 10)).status_code == 200
    assert push(b, batch("mock-user", "authorized", 501, role=True)).status_code == 200
    assert "account_unavailable" in login(a, iam).headers["location"]
    with Session(engine) as db:
        assert db.scalar(select(User).where(User.iam_subject == "mock-user")).status == "disabled"
        assert db.scalar(select(User).where(User.iam_subject == "mock-user")).user_sync_time == 10
        assert db.scalar(select(User).where(User.iam_subject == "mock-user")).role_sync_time == 501


@pytest.mark.parametrize("role", [False, True])
def test_recycle_unknown_resources_keeps_tombstones_against_late_pushes(auth, role):
    a, b, _, engine = auth
    status = "authorized" if role else "active"
    value = batch("future-user", status, 100, role=role, action="recycle")
    assert push(a, value).status_code == 200
    assert push(b, batch("future-user", status, 99, role=role)).status_code == 200
    with Session(engine) as db:
        if role:
            assert db.scalar(select(User).where(User.iam_subject == "future-user")).role == "user"
        else:
            assert (
                db.scalar(select(User).where(User.iam_subject == "future-user")).status
                == "disabled"
            )


def test_payload_deduplication_is_shared_and_same_time_conflicts_are_rejected(auth):
    a, b, _, engine = auth
    value = batch()
    first = push(a, value)
    assert first.status_code == 200
    duplicate = push(b, value)
    assert duplicate.json() == {**first.json(), "duplicate": True}
    assert a.get(PATH + "/" + first.json()["receiptId"], headers=HEADERS).json() == first.json()
    conflict = push(a, {**value, "action": "recycle"})
    assert conflict.status_code == 409 and conflict.json()["detail"] == "sync_version_conflict"
    with Session(engine) as db:
        receipts = db.scalars(select(IamSyncReceipt)).all()
        assert len(receipts) == 1
        assert len(db.scalars(select(OperationLog)).all()) == 1
        assert receipts[0].payload_hash
        assert db.scalar(select(User).where(User.iam_subject == "mock-user")).status == "active"


def test_same_version_is_unchanged_or_conflicting_and_older_batches_are_ignored(auth):
    a, b, _, _ = auth
    assert push(a, batch(status="disabled", version=200)).status_code == 200
    assert push(b, batch(status="disabled", version=200)).json()["duplicate"] is True
    conflict = push(b, batch(status="active", version=200))
    assert conflict.status_code == 409 and conflict.json()["detail"] == "sync_version_conflict"
    assert push(a, batch(status="active", version=199)).status_code == 200
    with Session(auth[3]) as db:
        user = db.scalar(select(User))
        assert (user.status, user.user_sync_time) == ("disabled", 200)


def test_conflicting_batch_rolls_back_state_revocations_and_receipt(auth):
    a, b, iam, engine = auth
    login(a, iam, subject="mock-admin")
    assert push(b, batch(version=100)).status_code == 200
    value = batch("mock-admin", "disabled", version=100)
    value["data"].append({"subject": "mock-user", "status": "disabled"})
    assert push(b, value).status_code == 409
    assert a.get("/api/v1/auth/session?scope=admin").status_code == 200

    with Session(engine) as db:
        assert db.scalar(select(User).where(User.iam_subject == "mock-admin")).status == "active"
        assert db.scalar(select(User).where(User.iam_subject == "mock-user")).status == "active"
        fingerprint = service.payload_hash(SyncBatch.model_validate(value))
        assert (
            db.scalar(select(IamSyncReceipt).where(IamSyncReceipt.payload_hash == fingerprint))
            is None
        )
        assert len(db.scalars(select(OperationLog).where(OperationLog.action == "sync")).all()) == 1


def test_disable_during_ticket_validation_blocks_callback_session_creation(auth, monkeypatch):
    a, b, iam, engine = auth
    callback = begin(a, iam, subject="mock-admin")
    validate = a.app.state.cas_client.validate

    def validate_then_disable(*args):
        principal = validate(*args)
        assert push(b, batch("mock-admin", "disabled")).status_code == 200
        return principal

    monkeypatch.setattr(a.app.state.cas_client, "validate", validate_then_disable)
    response = a.get(callback)
    assert "account_unavailable" in response.headers["location"]
    assert "ach_session" not in a.cookies
    with Session(engine) as db:
        assert db.scalar(select(AuthSession).where(AuthSession.revoked_at.is_(None))) is None


def test_uncertain_commit_can_be_retried_without_reapplying_changes(auth, monkeypatch):
    a, b, _, engine = auth
    commit = service.commit_write
    attempts = 0

    def lose_confirmation(db):
        nonlocal attempts
        attempts += 1
        commit(db)
        if attempts == 1:
            raise HTTPException(503, "commit_outcome_unknown")

    monkeypatch.setattr(service, "commit_write", lose_confirmation)
    value = batch(status="disabled")
    assert push(a, value).status_code == 503
    response = push(b, value)
    assert response.status_code == 200 and response.json()["duplicate"] is True
    assert attempts == 2
    with Session(engine) as db:
        assert len(db.scalars(select(IamSyncReceipt)).all()) == 1
        assert len(db.scalars(select(OperationLog)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"appId": "another-app"},
        {"resType": "org"},
        {"syncTime": "100"},
        {"syncTime": True},
        {"syncTime": 0},
        {"eventId": ""},
        {"eventId": "../etc"},
        {"data": []},
        {"data": [{"subject": "mock-user", "status": "active", "displayName": "  "}]},
        {"data": [{"subject": "mock-user", "status": 3000}]},
        {"data": [{"subject": "mock-user", "status": "active", "mobile": "sensitive-test-value"}]},
        {"data": [{"subject": "mock-user", "status": "active"}] * 2},
        {"data": [{"subject": "mock-user", "roleCode": "platform_admin", "status": "authorized"}]},
    ],
)
def test_mock_contract_rejects_ambiguous_or_excess_identity_data(auth, change):
    a, _, _, engine = auth
    result = push(a, {**batch(), **change})
    assert result.status_code == 422
    assert "sensitive-test-value" not in result.text
    with Session(engine) as db:
        assert db.scalar(select(IamSyncReceipt)) is None
        assert db.scalar(select(User)) is None


def test_source_content_and_batch_limits_are_enforced(auth):
    a, _, iam, _ = auth
    value = batch()
    assert a.post(PATH, json=value).status_code == 403
    login(a, iam, subject="mock-admin")
    assert a.post(PATH, json=value).status_code == 403
    assert a.get(PATH + "/missing").status_code == 403
    assert a.post(PATH, json=value, headers={"X-Mock-IAM-Key": "wrong"}).status_code == 403
    assert a.post(PATH, data={"value": "x"}, headers=HEADERS).status_code == 415
    assert (
        a.post(
            PATH, content=b"x" * 65537, headers={**HEADERS, "Content-Type": "application/json"}
        ).status_code
        == 413
    )
    value["data"] = [{"subject": f"user-{index}", "status": "active"} for index in range(101)]
    assert push(a, value).status_code == 422
    assert a.get(PATH + "/" + str(uuid4()), headers=HEADERS).status_code == 404


@pytest.mark.parametrize("environment", ["development", "production"])
def test_sync_endpoint_is_absent_when_mock_authentication_is_disabled(environment):
    settings = Settings(app_env=environment, database_url="sqlite://")
    with TestClient(create_app(settings)) as client:
        assert client.post(PATH, json=batch(), headers=HEADERS).status_code == 404


def test_mock_sync_openapi_exposes_a_usable_request_schema(auth):
    a, _, _, _ = auth
    operation = a.get("/openapi.json").json()["paths"][PATH]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert "$ref" not in json.dumps(schema)
    Draft202012Validator(schema).validate(batch())
    Draft202012Validator(schema).validate(batch(status="authorized", role=True))


def test_profile_changes_use_source_time_and_login_cannot_overwrite_them(auth):
    a, b, iam, engine = auth
    value = batch(version=200)
    value["data"][0]["displayName"] = "集团同步姓名"
    first = push(a, value)
    assert first.status_code == 200
    login(a, iam)
    assert a.get("/api/v1/auth/session").json()["user"]["displayName"] == "集团同步姓名"
    with Session(engine) as db:
        user = db.scalar(select(User))
        original_id, original_updated = user.id, user.updated_at
    # 新版本内容未变：推进防乱序时间，不制造资料更新时间和操作日志。
    assert push(b, {**value, "syncTime": 201}).status_code == 200
    assert push(b, value).json()["duplicate"] is True
    assert push(b, batch(version=199)).status_code == 200
    with Session(engine) as db:
        user = db.scalar(select(User))
        assert user.id == original_id and user.updated_at == original_updated
        assert user.display_name == "集团同步姓名" and user.user_sync_time == 201
        assert len(db.scalars(select(OperationLog).where(OperationLog.action == "sync")).all()) == 1
    value["data"][0]["displayName"] = "更新后姓名"
    assert push(b, {**value, "syncTime": 201}).status_code == 409
    assert push(b, {**value, "syncTime": 202}).status_code == 200
    assert a.get("/api/v1/auth/session").json()["user"]["displayName"] == "更新后姓名"


def test_canonical_payload_is_independent_of_json_key_order(auth):
    a, b, _, engine = auth
    value = batch()
    response = push(a, value)
    reordered = dict(reversed(list(value.items())))
    reordered["data"] = [{"status": "active", "subject": "mock-user"}]
    duplicate = push(b, reordered)
    assert duplicate.json() == {**response.json(), "duplicate": True}
    with Session(engine) as db:
        log = db.scalar(select(OperationLog))
        assert str(log.request_id) == response.headers["x-request-id"]
        assert log.actor_type == "iam" and log.user_id is None
        assert log.target_id == response.json()["receiptId"]


def test_synced_user_without_name_preserves_session_display_contract(auth):
    a, _, iam, engine = auth
    assert push(a, batch()).status_code == 200
    login(a, iam)
    # 已同步但未提供姓名时，展示身份编号；不让登录摘要覆盖权威资料。
    assert a.get("/api/v1/auth/session").json()["user"]["displayName"] == "mock-user"
    with Session(engine) as db:
        assert db.scalar(select(User)).display_name is None
