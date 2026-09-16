from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.modules.common.fixtures import fixture
from app.modules.common.schemas import FeedbackInput


def timestamp() -> str:
    """生成带 UTC 时区的接口时间文本。"""
    return datetime.now(UTC).isoformat()


def receipt(submit: bool = True) -> dict[str, str]:
    """生成模拟受理回执，submit 为真时标记待审核。"""
    return {"id": "mock-" + uuid4().hex[:12], "status": "pending" if submit else "draft"}


def list_feedback() -> list[dict]:
    """返回固定反馈样例，补齐当前接口要求的展示字段。"""
    return [
        {
            "ownerName": "示例提交人",
            "contact": "",
            "attachmentName": "",
            "reply": "",
            **item,
            "createdAt": item["createdAt"].replace(" ", "T") + ":00+08:00",
        }
        for item in fixture("common", "feedback")
    ]


def feedback_receipt(payload: FeedbackInput) -> dict:
    """生成反馈模拟回执，仅回显本次请求，不持久化业务状态。"""
    # 只回显请求结果；没有业务表、审计、幂等记录或跨端反馈状态流转。
    return {
        **payload.model_dump(by_alias=True),
        "id": receipt()["id"],
        "ownerName": "示例提交人",
        "createdAt": timestamp(),
        "status": "处理中",
        "reply": "",
    }


def operations(token_period: str, ranking_type: str) -> dict:
    """按时间周期和排行类型读取固定运营样例。"""
    variants = fixture("common", "operations")
    return variants.get(token_period + ":" + ranking_type, variants["月:用户活跃榜"])
