from fastapi import HTTPException

from app.modules.admin.schemas import FeedbackQuery, FeedbackUpdate
from app.modules.common.fixtures import paginate
from app.modules.common.service import list_feedback, timestamp


def overview():
    items = list_feedback()
    resolved = sum(item["status"] == "已解决" for item in items)
    return {"total": len(items), "open": len(items) - resolved, "resolved": resolved}


def feedback_page(query: FeedbackQuery):
    state = {"all": None, "open": "处理中", "resolved": "已解决"}[query.status]
    items = [
        item
        for item in list_feedback()
        if (state is None or item["status"] == state)
        and query.q.casefold() in item["title"].casefold()
    ]
    return paginate(items, query.page, query.pageSize)


def preview_update(record_id: str, payload: FeedbackUpdate):
    record = next((item for item in list_feedback() if item["id"] == record_id), None)
    if record is None:
        raise HTTPException(404, "sample_feedback_not_found")
    return {**record, **payload.model_dump(), "updatedAt": timestamp()}
