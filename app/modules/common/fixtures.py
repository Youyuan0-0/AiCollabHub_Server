"""Immutable samples for HTTP integration; never connects to a business database."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path


@lru_cache
def _load(module: str, name: str) -> dict | list:
    """读取并缓存服务端指定的 JSON 固定样例。"""
    path = Path(__file__).resolve().parents[1] / module / "fixtures" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(module: str, name: str) -> dict | list:
    """读取固定样例的独立副本；模块和文件名仅由服务端代码提供。"""
    # module/name 仅由模块代码提供；每次返回副本，各 worker 及重复请求使用同一份样例。
    return deepcopy(_load(module, name))


def paginate(items: list, page: int, page_size: int) -> dict:
    """按从 1 开始的页码截取列表，返回总数和 camelCase 分页信息。"""
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": len(items),
        "page": page,
        "pageSize": page_size,
    }
