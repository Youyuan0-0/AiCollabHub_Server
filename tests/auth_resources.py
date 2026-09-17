"""认证测试的共享 Redis；执行真实 Lua 脚本且不访问开发服务。"""

from fakeredis import FakeRedis

from app.cache.client import RedisCache
from app.core.resources import create_resources


def auth_resources_factory():
    """两个测试 API 实例共享独立缓存，用于验证回调的原子消费。"""
    client = FakeRedis(decode_responses=True)

    def create(settings):
        resources = create_resources(settings)
        resources.cache = RedisCache(client, namespace="auth:test", default_ttl=300)
        return resources

    return create
