from fastapi import APIRouter, HTTPException, Request

from app.modules.common import service
from app.modules.common.catalog import find_sample
from app.modules.common.schemas import AccessInput, FeedbackInput, InvocationInput, PublicationInput

router = APIRouter(tags=["common (mock)"])


@router.get("/framework")
def framework(request: Request):
    return {
        "mode": "mock",
        "instance": request.app.state.settings.instance_id,
        "modules": ["datasets", "models", "applications", "knowledge", "tools", "common", "admin"],
    }


# 看板由 app.api.overview 读取数据库快照，避免相同路径仍命中固定 Mock。


@router.get("/feedback")
def feedback():
    return service.list_feedback()


@router.post("/feedback", status_code=201)
def submit_feedback(payload: FeedbackInput):
    return service.feedback_receipt(payload)


@router.post("/resources/{resource_id}/invoke")
def invoke(resource_id: str, payload: InvocationInput):
    resource = find_sample(resource_id)
    return {
        "id": service.receipt()["id"],
        "durationMs": 0,
        "text": f"{resource['name']} 的模拟响应：{payload.prompt[:200]}",
    }


@router.post("/resources/{resource_id}/access-requests", status_code=201)
def access_request(resource_id: str, payload: AccessInput):
    find_sample(resource_id)
    return service.receipt()


@router.post("/publications", status_code=201)
def publication(payload: PublicationInput):
    if payload.submit and (not payload.name.strip() or not payload.summary.strip()):
        raise HTTPException(422, "name_and_summary_required")
    return service.receipt(payload.submit)
