"""Immutable samples for HTTP integration; never connects to a business database."""

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path


@lru_cache
def _load(module: str, name: str):
    path = Path(__file__).resolve().parents[1] / module / "fixtures" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def fixture(module: str, name: str):
    # module/name 仅由模块代码提供；每次返回副本，A/B 实例及重复请求都使用同一份样例。
    return deepcopy(_load(module, name))


def paginate(items: list, page: int, page_size: int):
    return {
        "items": items[(page - 1) * page_size : page * page_size],
        "total": len(items),
        "page": page,
        "pageSize": page_size,
    }
