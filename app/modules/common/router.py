from fastapi import APIRouter, HTTPException, Request

from app.modules.common import service
from app.modules.common.catalog import find_sample
from app.modules.common.fixtures import fixture
from app.modules.common.schemas import AccessInput, FeedbackInput, InvocationInput, PublicationInput

router = APIRouter(tags=["common (mock)"])


@router.get("/framework")
def framework(request: Request):
    return {
        "mode": "mock",
        "instance": request.app.state.settings.instance_id,
        "modules": ["datasets", "models", "applications", "knowledge", "tools", "common", "admin"],
    }


@router.get("/overview/home")
def home():
    return fixture("common", "home")


@router.get("/overview/operations")
def operations(tokenPeriod: str = "月", rankingType: str = "用户活跃榜"):
    return service.operations(tokenPeriod, rankingType)


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
    if payload.kind == "application":
        raise HTTPException(422, "请通过场景应用发布页面提交完整资料")
    if payload.submit and (not payload.name.strip() or not payload.summary.strip()):
        raise HTTPException(422, "name_and_summary_required")
    return service.receipt(payload.submit)
