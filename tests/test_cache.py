from unittest.mock import Mock

import pytest
from redis import Redis
from redis.exceptions import ConnectionError

from app.cache.client import RedisCache
from app.core.config import Settings


@pytest.fixture
def client():
    storage = {}
    client = Mock(spec=Redis)
    client.get.side_effect = storage.get

    def store(key, value, *, ex):
        storage[key] = value
        return True

    def delete(*keys):
        for key in keys:
            storage.pop(key, None)

    client.set.side_effect = store
    client.delete.side_effect = delete
    return client


def test_cache_json_ttl_and_cross_instance_invalidation(client):
    first = RedisCache(client, namespace="platform:test", default_ttl=60)
    second = RedisCache(client, namespace="platform:test", default_ttl=60)
    value = {"title": "资源目录", "versions": [1, 2], "enabled": True}
    assert first.set_json("catalog:public", value)
    assert client.set.call_args.kwargs["ex"] == 60
    assert second.get_json("catalog:public") == value
    assert second.delete("catalog:public")
    assert first.get_json("catalog:public") is None


def test_environment_namespaces_do_not_share_values(client):
    development = RedisCache(client, namespace="platform:development", default_ttl=60)
    production = RedisCache(client, namespace="platform:production", default_ttl=60)
    development.set_json("catalog", {"id": 1}, ttl=10)
    assert client.set.call_args.kwargs["ex"] == 10
    assert production.get_json("catalog") is None


@pytest.mark.parametrize("ttl", [0, -1, 1.5, True])
def test_cache_rejects_non_expiring_or_invalid_ttl(client, ttl):
    cache = RedisCache(client, namespace="platform:test", default_ttl=60)
    with pytest.raises(ValueError, match="positive integer"):
        cache.set_json("key", {}, ttl=ttl)
    client.set.assert_not_called()


def test_corrupt_json_is_invalidated(client):
    cache = RedisCache(client, namespace="platform:test", default_ttl=60)
    client.get.side_effect = None
    client.get.return_value = "corrupt-json"
    assert cache.get_json("catalog") is None
    client.delete.assert_called_once_with("platform:test:catalog")


def test_invalid_utf8_is_a_cache_miss_and_is_invalidated(client):
    cache = RedisCache(client, namespace="platform:test", default_ttl=60)
    client.get.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    assert cache.get_json("catalog") is None
    client.delete.assert_called_once_with("platform:test:catalog")


def test_unavailable_cache_returns_miss_or_failure_without_secrets(client, caplog):
    cache = RedisCache(client, namespace="platform:test", default_ttl=60)
    for method in (client.get, client.set, client.delete, client.ping):
        method.side_effect = ConnectionError("redis://:private-password@internal-host")
    assert cache.get_json("catalog") is None
    assert not cache.set_json("catalog", {})
    assert not cache.delete("catalog")
    assert not cache.probe()
    assert "private-password" not in caplog.text
    assert "internal-host" not in caplog.text


def test_cache_factory_is_lazy_and_owns_its_connection_pool(monkeypatch):
    ping = Mock(side_effect=AssertionError("must not connect while configuring"))
    monkeypatch.setattr(Redis, "ping", ping)
    cache = RedisCache.from_settings(Settings(redis_url="redis://localhost:6379/0"))
    try:
        assert cache.namespace == "aicollabhub:development"
        assert cache.client.connection_pool.max_connections == 20
        assert cache.client.connection_pool.connection_kwargs["socket_timeout"] == 1
        ping.assert_not_called()
    finally:
        cache.close()
