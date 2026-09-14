from datetime import UTC, datetime
from uuid import uuid4

from app.modules.common.fixtures import fixture
from app.modules.common.schemas import FeedbackInput


def timestamp():
    return datetime.now(UTC).isoformat()


def receipt(submit=True):
    return {"id": "mock-" + uuid4().hex[:12], "status": "pending" if submit else "draft"}


def list_feedback():
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


def feedback_receipt(payload: FeedbackInput):
    # 只回显请求结果；没有业务表、审计、幂等记录或跨端反馈状态流转。
    return {
        **payload.model_dump(by_alias=True),
        "id": receipt()["id"],
        "ownerName": "示例提交人",
        "createdAt": timestamp(),
        "status": "处理中",
        "reply": "",
    }


def operations(token_period: str, ranking_type: str):
    variants = fixture("common", "operations")
    return variants.get(token_period + ":" + ranking_type, variants["月:用户活跃榜"])
