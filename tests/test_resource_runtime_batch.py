"""按编号批量读取运行态，缺失、错误和模块边界不能混淆。"""

from hashlib import sha256
from unittest.mock import Mock

import pytest
from fakeredis import FakeRedis
from fastapi import HTTPException
from redis.exceptions import RedisError

from app.cache.client import RedisCache
from app.modules.common.resource_runtime import ResourceRuntime


@pytest.fixture
def runtime():
    client = FakeRedis(decode_responses=True)
    yield ResourceRuntime(RedisCache(client, namespace="batch-test", default_ttl=60), "knowledge")
    client.close()


def test_get_many_batches_scoped_keys_without_single_get(runtime, monkeypatch):
    for index in range(150):
        runtime.put("index", str(index), {"index": index})
    runtime.put("unrelated", "0", {"secret": "not requested"})
    mget = Mock(wraps=runtime.cache.client.mget)
    monkeypatch.setattr(runtime.cache.client, "mget", mget)
    monkeypatch.setattr(runtime.cache.client, "get", Mock(side_effect=AssertionError("single GET")))
    result = runtime.get_many("index", [str(index) for index in range(150)] + ["0", "expired"])
    assert result == {**{str(index): {"index": index} for index in range(150)}, "expired": None}
    assert [len(call.args[0]) for call in mget.call_args_list] == [100, 51]
    mget.reset_mock()
    assert runtime.get_many("index", []) == {}
    mget.assert_not_called()


@pytest.mark.parametrize("error", [RedisError("unavailable"), ValueError("malformed")])
def test_get_many_failure_is_not_treated_as_expiry(runtime, monkeypatch, error):
    monkeypatch.setattr(runtime.cache.client, "mget", Mock(side_effect=error))
    with pytest.raises(HTTPException) as caught:
        runtime.get_many("index", ["base"])
    assert caught.value.status_code == 503


def test_get_many_rejects_corrupted_json(runtime):
    runtime.cache.client.set(runtime._prefix("index") + sha256(b"base").hexdigest(), "invalid")
    with pytest.raises(HTTPException) as caught:
        runtime.get_many("index", ["base"])
    assert caught.value.status_code == 503
