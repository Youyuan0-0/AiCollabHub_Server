import hashlib
import hmac
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException


def digest(value: str) -> str:
    """生成令牌或票据的 SHA-256 摘要，供数据库保存与匹配。"""
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token: str, secret: str) -> str:
    """使用服务端密钥生成绑定当前会话令牌的 CSRF 校验值。"""
    return hmac.new(secret.encode(), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


def safe_return_path(value: str, scope: str) -> str:
    """验证登录返回地址属于当前入口的站内路径，非法地址返回 400。"""
    decoded = value
    # 检查多层编码后的路径，识别被编码隐藏的双斜线、反斜线和路径跳转片段。
    # 解码只用于校验；通过后保留原值，避免重新编码时改变查询参数含义。
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if (
        len(value) > 2048
        or not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
    ):
        raise HTTPException(400, "invalid_return_path")
    path = urlsplit(decoded).path
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise HTTPException(400, "invalid_return_path")
    if scope == "admin":
        valid = path.startswith("/admin/") and path != "/admin/login"
    else:
        valid = not (path in ("/admin", "/login", "/api") or path.startswith(("/admin/", "/api/")))
    if not valid:
        raise HTTPException(400, "invalid_return_path")
    return value
