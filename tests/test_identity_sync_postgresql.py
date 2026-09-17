"""显式指定测试连接后，在独立临时 Schema 中验证 PostgreSQL 并发与事务。"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from auth_resources import auth_resources_factory
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema
from test_auth import SECRET, begin, csrf, login
from test_identity_sync import batch, push
from test_identity_sync import (
    test_conflicting_batch_rolls_back_state_revocations_and_receipt as check_rollback,
)

from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_db_engine
from app.main import create_app
from app.modules.common.auth import router as auth_router
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.mock_iam import create_mock_iam
from app.modules.common.auth.models import AuthSession, OperationLog, User
from app.modules.common.identity_sync import service
from app.modules.common.identity_sync.models import IamSyncReceipt

pytestmark = pytest.mark.skipif(
    not os.environ.get("IDENTITY_SYNC_TEST_DATABASE_URL"),
    reason="requires an explicitly configured PostgreSQL test connection",
)


@pytest.fixture
def postgres_auth():
    url = make_url(os.environ["IDENTITY_SYNC_TEST_DATABASE_URL"])
    if url.get_backend_name() != "postgresql":
        pytest.fail("Identity concurrency tests require PostgreSQL")
    schema = "identity_sync_test_" + uuid4().hex
    admin = create_db_engine(url, pool_size=1, max_overflow=0)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    test_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    engine = create_db_engine(test_url, pool_size=1, max_overflow=0)
    settings = Settings(
        app_env="test",
        auth_mode="mock",
        auth_public_origin="http://testserver",
        auth_mock_iam_url="http://iam.test",
        auth_mock_secret=SECRET,
        database_url=test_url.render_as_string(hide_password=False),
        db_pool_size=2,
        db_max_overflow=0,
    )
    try:
        Base.metadata.create_all(engine)
        cas = MockCasClient(settings)
        cas.client.close()
        resources = auth_resources_factory()
        with (
            TestClient(
                create_app(settings, cas_client=cas, resources_factory=resources),
                follow_redirects=False,
            ) as a,
            TestClient(
                create_app(
                    settings.model_copy(update={"instance_id": "B"}),
                    cas_client=cas,
                    resources_factory=resources,
                ),
                follow_redirects=False,
            ) as b,
            TestClient(
                create_mock_iam(settings, notification_client=b),
                base_url="http://iam.test",
                headers={"X-Mock-IAM-Key": SECRET},
                follow_redirects=False,
            ) as iam,
        ):
            cas.client = iam
            yield a, b, iam, engine
    finally:
        engine.dispose()
        # 名称仅来自本测试生成的 UUID，清理范围限定为这次创建的临时 Schema。
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def test_postgresql_concurrent_duplicate_and_out_of_order_events(postgres_auth):
    a, b, _, engine = postgres_auth
    barrier = Barrier(2)
    value = batch()

    def send(client, payload):
        barrier.wait(timeout=10)
        return push(client, payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(send, client, value) for client in (a, b)]
        responses = [future.result(timeout=20) for future in futures]
        assert all(response.status_code == 200 for response in responses)
        assert sorted(response.json()["duplicate"] for response in responses) == [False, True]
        futures = [
            executor.submit(send, a, batch(status="active", version=101)),
            executor.submit(send, b, batch(status="disabled", version=102)),
        ]
        assert all(future.result(timeout=20).status_code == 200 for future in futures)
    with Session(engine) as db:
        identity = db.scalar(select(User).where(User.iam_subject == "mock-user"))
        assert identity.status == "disabled" and identity.user_sync_time == 102
        receipts = db.scalars(select(IamSyncReceipt)).all()
        assert len(receipts) == 3
        assert len(db.scalars(select(OperationLog).where(OperationLog.action == "sync")).all()) == 2


@pytest.mark.parametrize("first", ["login", "sync"])
def test_postgresql_login_and_disable_serialize_on_the_identity(postgres_auth, monkeypatch, first):
    a, b, iam, engine = postgres_auth
    assert push(b, batch(version=1)).status_code == 200
    callback = begin(a, iam)
    locked, release, contender_started = Event(), Event(), Event()
    original = service.lock_identity

    def hold(db, subject):
        identity = original(db, subject)
        locked.set()
        assert release.wait(timeout=10)
        return identity

    def contend(db, subject):
        contender_started.set()
        return original(db, subject)

    monkeypatch.setattr(auth_router, "lock_identity", hold if first == "login" else contend)
    monkeypatch.setattr(service, "lock_identity", hold if first == "sync" else contend)
    with ThreadPoolExecutor(max_workers=2) as executor:
        try:
            if first == "login":
                login_future = executor.submit(a.get, callback)
                assert locked.wait(timeout=10)
                sync_future = executor.submit(push, b, batch(status="disabled", version=2))
            else:
                sync_future = executor.submit(push, b, batch(status="disabled", version=2))
                assert locked.wait(timeout=10)
                login_future = executor.submit(a.get, callback)
            assert contender_started.wait(timeout=10)
        finally:
            release.set()
        sync_response = sync_future.result(timeout=20)
        login_response = login_future.result(timeout=20)
    assert sync_response.status_code == 200, sync_response.text
    assert login_response.status_code == 303
    if first == "sync":
        assert "account_unavailable" in login_response.headers["location"]
        assert "ach_session" not in a.cookies
    else:
        assert login_response.headers["location"] == "/datasets"
        with Session(engine) as db:
            assert db.scalar(select(AuthSession)).revoked_at is not None
    assert a.get("/api/v1/auth/session").status_code == 401


def test_postgresql_failed_batch_rolls_back_the_whole_transaction(postgres_auth):
    check_rollback(postgres_auth)


def test_postgresql_concurrent_logout_writes_one_success_log(postgres_auth):
    a, b, iam, engine = postgres_auth
    login(a, iam)
    b.cookies.update(a.cookies)
    headers = csrf(a)
    barrier = Barrier(2)

    def logout(client):
        barrier.wait(timeout=10)
        return client.post("/api/v1/auth/logout?global_logout=false", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(logout, (a, b)))
    assert all(response.status_code == 200 for response in responses)
    with Session(engine) as db:
        assert (
            len(db.scalars(select(OperationLog).where(OperationLog.action == "logout")).all()) == 1
        )


def test_postgresql_first_logins_share_one_user_identity(postgres_auth, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from app.modules.common.auth.cas import Principal

    a, b, _, engine = postgres_auth
    callbacks = []
    for suffix, client in (("a", a), ("b", b)):
        response = client.get("/api/v1/auth/login")
        callback = parse_qs(urlsplit(response.headers["location"]).query)["service"][0]
        callbacks.append(callback + "&ticket=ST-first-" + suffix)
    monkeypatch.setattr(
        a.app.state.cas_client, "validate", lambda *args: Principal("first-user", "首次登录")
    )
    barrier = Barrier(2)

    def finish(pair):
        client, callback = pair
        barrier.wait(timeout=10)
        return client.get(callback)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(finish, zip((a, b), callbacks, strict=True)))
    assert all(
        response.status_code == 303 and response.headers["location"] == "/"
        for response in responses
    )
    with Session(engine) as db:
        user = db.scalars(select(User)).one()
        sessions = db.scalars(select(AuthSession)).all()
        assert len(sessions) == 2 and all(session.user_id == user.id for session in sessions)
