from __future__ import annotations

from fastapi import HTTPException

from app.modules.admin.schemas import FeedbackQuery, FeedbackUpdate
from app.modules.common.fixtures import paginate
from app.modules.common.service import list_feedback, timestamp


def overview() -> dict[str, int]:
    """统计固定反馈样例的总数、处理中数量和已解决数量。"""
    items = list_feedback()
    resolved = sum(item["status"] == "已解决" for item in items)
    return {"total": len(items), "open": len(items) - resolved, "resolved": resolved}


def feedback_page(query: FeedbackQuery) -> dict:
    """按状态与标题关键词筛选固定反馈样例并分页。"""
    state = {"all": None, "open": "处理中", "resolved": "已解决"}[query.status]
    items = [
        item
        for item in list_feedback()
        if (state is None or item["status"] == state)
        and query.q.casefold() in item["title"].casefold()
    ]
    return paginate(items, query.page, query.page_size)


def preview_update(record_id: str, payload: FeedbackUpdate) -> dict:
    """生成反馈修改预览；不将模拟修改持久化。"""
    record = next((item for item in list_feedback() if item["id"] == record_id), None)
    if record is None:
        raise HTTPException(404, "sample_feedback_not_found")
    return {**record, **payload.model_dump(), "updatedAt": timestamp()}
