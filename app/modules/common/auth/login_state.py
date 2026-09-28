import json
import logging
from dataclasses import asdict, dataclass

from fastapi import HTTPException, Request
from redis.exceptions import RedisError

from app.cache.client import get_cache

logger = logging.getLogger(__name__)

# 浏览器绑定不符时保留请求，避免攻击者提前消费其他浏览器的登录上下文。
# 校验和删除必须在同一段 Lua 中执行；拆成 GET/DEL 会让并发回调同时通过一次性校验。
CONSUME_LOGIN = """
local value = redis.call('GET', KEYS[1])
if not value then return nil end
local context = cjson.decode(value)
if context.browser_hash ~= ARGV[1] or context.scope ~= ARGV[2] then return nil end
redis.call('DEL', KEYS[1])
return value
"""


@dataclass(frozen=True)
class LoginState:
    """仅在 Redis 中短期存在的一次性浏览器登录上下文。

    browser_hash 绑定发起登录的浏览器，scope 区分用户端和管理端；service_url
    固定本次 CAS 校验地址，return_path 保存已校验的站内跳转路径。跨 worker
    回调从 Redis 读取同一份上下文，不能以进程内字典替代。
    """

    browser_hash: str
    scope: str
    service_url: str
    return_path: str


def state_key(request: Request, state_hash: str) -> str:
    """使用环境隔离的认证键，独立于可回退的普通业务缓存。"""
    cache = get_cache(request)
    if cache is None:
        logger.warning(
            "Login state storage is not configured request_id=%s", request.state.request_id
        )
        raise HTTPException(503, "login_state_unavailable")
    return f"{cache.namespace}:auth:login:{state_hash}"


def save_login_state(request: Request, state_hash: str, state: LoginState) -> None:
    """保存有 TTL 的登录上下文；Redis 不可用时不开始登录。"""
    key = state_key(request, state_hash)
    try:
        saved = get_cache(request).client.set(
            key, json.dumps(asdict(state)), ex=request.app.state.settings.auth_login_ttl, nx=True
        )
    except RedisError as error:
        logger.warning(
            "Login state save failed (%s) request_id=%s",
            type(error).__name__,
            request.state.request_id,
        )
        raise HTTPException(503, "login_state_unavailable") from None
    if not saved:
        raise HTTPException(503, "login_state_unavailable")


def consume_login_state(
    request: Request, state_hash: str, browser_hash: str, scope: str
) -> LoginState | None:
    """在 Redis 原子校验绑定并消费；错误浏览器、过期和重复请求均不通过。"""
    key = state_key(request, state_hash)
    try:
        value = get_cache(request).client.eval(CONSUME_LOGIN, 1, key, browser_hash, scope)
        return LoginState(**json.loads(value)) if value else None
    except (RedisError, ValueError, TypeError) as error:
        logger.warning(
            "Login state consume failed (%s) request_id=%s",
            type(error).__name__,
            request.state.request_id,
        )
        raise HTTPException(503, "login_state_unavailable") from None
