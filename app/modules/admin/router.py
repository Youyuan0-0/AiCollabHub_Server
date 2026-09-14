from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.admin import service
from app.modules.admin.schemas import FeedbackQuery, FeedbackUpdate

router = APIRouter(prefix="/admin", tags=["admin (mock)"])


@router.get("/overview")
def overview():
    return service.overview()


@router.get("/feedback")
def feedback(query: Annotated[FeedbackQuery, Query()]):
    return service.feedback_page(query)


@router.patch("/feedback/{record_id}")
def update_feedback(record_id: str, payload: FeedbackUpdate):
    return service.preview_update(record_id, payload)
