"""个人消息读取和原子批量已读，不依赖真实 Redis 或外部 IAM。"""

from hashlib import sha256
from unittest.mock import Mock

import pytest
from fakeredis import FakeRedis
from fastapi import HTTPException
from redis.exceptions import RedisError
from test_auth import auth as auth
from test_auth import csrf, login

from app.cache.client import RedisCache
from app.modules.common.identity import Actor
from app.modules.common.resource_runtime import ResourceRuntime
from app.modules.profile.messages import ProfileMessagesService

ACTOR = Actor("writer", "测试用户", "publisher", "org-a")


@pytest.fixture
def runtime():
    client = FakeRedis(decode_responses=True)
    yield ResourceRuntime(
        RedisCache(client, namespace="profile-tests", default_ttl=60), "application", ttl=60
    )
    client.close()


def notice(key, recipient="writer", kind="review", application="app-1"):
    return {
        "id": key,
        "recipient": recipient,
        "kind": kind,
        "applicationId": application,
        "title": "审核结果",
        "read": False,
        "createdAt": "2026-09-28T00:00:00Z",
    }


def test_runtime_lists_in_batches_and_ignores_expired_duplicates(runtime, monkeypatch):
    for index in range(150):
        runtime.put("notifications", str(index), notice(str(index)))
    keys = list(runtime.cache.client.scan_iter(match=runtime._prefix("notifications") + "*"))
    monkeypatch.setattr(
        runtime.cache.client, "scan_iter", lambda **kwargs: iter(keys + keys[:5] + ["missing"])
    )
    original = runtime.cache.client.mget
    calls = []

    def mget(batch):
        calls.append(len(batch))
        return original(batch)

    monkeypatch.setattr(runtime.cache.client, "mget", mget)
    result = runtime.list("notifications")
    assert len(result) == 150
    assert len({item["id"] for item in result}) == 150
    assert calls == [100, 51]


def test_personal_messages_only_resolve_referenced_updates(runtime):
    for item in [
        notice("own"),
        notice("other", "reviewer"),
        notice("warning", kind="run_failure"),
        notice("visible", kind="new_version"),
        notice("hidden", kind="new_version", application="app-2"),
    ]:
        runtime.put("notifications", item["id"], item)
    resources = Mock()
    resources.visible_application_ids.return_value = {"app-1"}
    service = ProfileMessagesService(resources, runtime)
    result = service.list_for_user(ACTOR)
    assert {item["id"] for item in result} == {"own", "visible"}
    assert {item["id"]: item["href"] for item in result} == {
        "own": "/profile?tab=publications&kind=application",
        "visible": "/applications?resourceId=app-1",
    }
    resources.visible_application_ids.assert_called_once_with(ACTOR, {"app-1", "app-2"})
    assert all("recipient" not in item for item in result)


def test_batch_read_is_atomic_and_preserves_expiration(runtime):
    for key, recipient in [("first", "writer"), ("second", "writer"), ("foreign", "reviewer")]:
        runtime.put("notifications", key, notice(key, recipient))
    cache_key = runtime._prefix("notifications") + sha256(b"first").hexdigest()
    runtime.cache.client.pexpire(cache_key, 20000)
    before = runtime.cache.client.pttl(cache_key)
    for bad in ("foreign", "expired"):
        with pytest.raises(HTTPException) as error:
            runtime.mark_notifications_read("writer", ["first", bad])
        assert error.value.status_code == 404
        assert runtime.get("notifications", "first")["read"] is False
    assert runtime.mark_notifications_read("writer", ["first", "second", "first"]) == [
        "first",
        "second",
    ]
    assert runtime.get("notifications", "first")["read"] is True
    assert runtime.get("notifications", "second")["read"] is True
    assert runtime.get("notifications", "foreign")["read"] is False
    assert 0 < runtime.cache.client.pttl(cache_key) <= before
    assert runtime.get("notifications", "expired") is None


def test_redis_failure_never_looks_like_empty_or_success(runtime, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RedisError("unavailable")

    monkeypatch.setattr(runtime.cache.client, "scan_iter", unavailable)
    monkeypatch.setattr(runtime.cache.client, "eval", unavailable)
    for call in (
        lambda: runtime.list("notifications"),
        lambda: runtime.mark_notifications_read("writer", ["first"]),
    ):
        with pytest.raises(HTTPException) as error:
            call()
        assert error.value.status_code == 503


def test_profile_routes_enforce_login_csrf_and_message_ownership(auth):
    client, _, iam, _ = auth
    for path in ("", "/favorites", "/subscriptions", "/messages"):
        assert client.get("/api/v1/profile" + path).status_code == 401
    read_path = "/api/v1/profile/messages/read"
    assert client.put(read_path, json={"ids": ["own"]}).status_code == 401
    login(client, iam, subject="mock-user")
    repository = client.app.state.application_service.repository
    with repository.transaction() as session:
        for item in [notice("own", "mock-user"), notice("foreign", "mock-user-c")]:
            session.put_entry("notifications", item["id"], item)
    response = client.get("/api/v1/profile/messages")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["own"]
    assert client.get("/api/v1/profile/subscriptions").status_code == 200
    assert client.put(read_path, json={"ids": ["own"]}).status_code == 403
    for path in ("/favorites/application/app-1", "/subscriptions/app-1"):
        assert client.delete("/api/v1/profile" + path).status_code == 403
    headers = csrf(client)
    assert (
        client.put(read_path, json={"ids": ["own", "foreign"]}, headers=headers).status_code == 404
    )
    assert client.get("/api/v1/profile/messages").json()[0]["read"] is False
    for ids in ([], [str(index) for index in range(101)]):
        assert client.put(read_path, json={"ids": ids}, headers=headers).status_code == 422
    response = client.put(read_path, json={"ids": ["own", "own"]}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"readIds": ["own"]}
    with repository.transaction(is_readonly=True) as session:
        assert session.entry("notifications", "own")["read"] is True
        assert session.entry("notifications", "foreign")["read"] is False
