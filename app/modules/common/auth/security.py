import hashlib
import hmac
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


def safe_return_path(value: str, scope: str) -> str:
    decoded = value
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
