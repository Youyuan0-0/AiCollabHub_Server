from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.common.fixtures import fixture


def timestamp() -> str:
    """生成带 UTC 时区的接口时间文本。"""
    return datetime.now(UTC).isoformat()


def receipt(submit: bool = True) -> dict[str, str]:
    """生成模拟受理回执，submit 为真时标记待审核。"""
    return {"id": "mock-" + uuid4().hex[:12], "status": "pending" if submit else "draft"}


def operations(token_period: str, ranking_type: str) -> dict:
    """按时间周期和排行类型读取固定运营样例。"""
    variants = fixture("common", "operations")
    return variants.get(token_period + ":" + ranking_type, variants["月:用户活跃榜"])
