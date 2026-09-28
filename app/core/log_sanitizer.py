"""限制系统日志上下文大小，并清除凭据和原始请求内容。"""

import math
import re
from pathlib import Path
from traceback import extract_tb
from uuid import UUID

MAX_TEXT_LENGTH = 2000
MAX_DETAILS_LENGTH = 16000
MAX_CONTEXT_ITEMS = 100
REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"password|passwd|pwd|secret|token|cookie|authorization|credential|"
    r"ticket|signature|apikey|accesskey|privatekey|headers|body|payload",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
AUTH_PATTERN = re.compile(r"\b(?:Bearer|Basic)\s+[^\s,;\"']+", re.IGNORECASE)
HEADER_PATTERN = re.compile(
    r"\b(?:set-cookie|cookie|authorization|proxy-authorization)\s*[:=][^\r\n]*",
    re.IGNORECASE,
)
SECRET_PATTERN = re.compile(
    r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|token|cookie|authorization|credential|"
    r"ticket|signature|api[_-]?key|access[_-]?key)[\w-]*)[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*(?:\"|$)|'[^']*(?:'|$)|[^\s,;&}]+)"
)


def redact_text(value: str) -> str:
    """隐藏 URL、认证头和常见凭据赋值，再限制文本长度。"""
    # 先限制扫描量，清理 PostgreSQL text/JSONB 不接受的 NUL 和孤立代理码点。
    value = value[: MAX_TEXT_LENGTH * 4].encode("utf-8", errors="replace").decode("utf-8")
    value = value.replace("\x00", "[NUL]")
    value = HEADER_PATTERN.sub(REDACTED, value)
    value = URL_PATTERN.sub("[REDACTED_URL]", value)
    value = AUTH_PATTERN.sub(REDACTED, value)
    value = SECRET_PATTERN.sub(lambda match: f"{match[1]}={REDACTED}", value)
    return value[:MAX_TEXT_LENGTH]


def sanitize_value(value: object) -> object:
    """把日志上下文转换为经过脱敏、具有深度及总元素上限的 JSON 值。

    整棵结构共用元素预算，防止多个嵌套集合放大日志体积。未知对象只保留类型名，
    不调用可能包含凭据的 __str__/__repr__；异常详情由专门的受限提取函数处理。
    """
    return _sanitize(value, depth=0, budget=[MAX_CONTEXT_ITEMS])


def _sanitize(value: object, *, depth: int, budget: list[int]) -> object:
    """递归共用总元素预算，避免嵌套集合绕过单层数量上限。"""
    if budget[0] <= 0:
        return "[TRUNCATED]"
    budget[0] -= 1
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() < 64 else "[NUMBER_TOO_LARGE]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, UUID):
        return str(value)
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 30 or budget[0] <= 0:
                result["truncated"] = True
                break
            if not isinstance(key, str):
                continue
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            result[redact_text(key)[:100]] = (
                REDACTED
                if SENSITIVE_KEY.search(normalized_key)
                else _sanitize(item, depth=depth + 1, budget=budget)
            )
        return result
    if isinstance(value, (tuple, list)):
        return [_sanitize(item, depth=depth + 1, budget=budget) for item in value[:30]]
    return f"[{type(value).__name__}]"


def exception_details(error: BaseException) -> dict[str, object]:
    """仅记录异常类型和调用位置，避免异常文本、源码行或局部变量泄露凭据。"""
    return {
        "errorType": type(error).__name__,
        "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in extract_tb(error.__traceback__)[-15:]
        ],
    }
