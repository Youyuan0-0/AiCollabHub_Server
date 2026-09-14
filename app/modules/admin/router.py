from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.admin import service
from app.modules.admin.schemas import FeedbackQuery, FeedbackUpdate
from app.modules.applications.admin_queries import ApplicationReviewQuery, query_publications
from app.modules.applications.application_service import application_service
from app.modules.applications.configuration import SaveApplicationConfiguration
from app.modules.applications.identity import current_actor, require_admin
from app.modules.applications.publication_schemas import (
    ApplicationVisibility,
    ReviewApplicationPublication,
)
from app.modules.common.catalog import query_resources
from app.modules.common.schemas import CatalogQuery

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


@router.get("/applications")
def applications(request: Request, query: Annotated[CatalogQuery, Query()]):
    return query_resources(
        application_service(request).resources(current_actor(request, True), admin=True), query
    )


@router.get("/applications/{application_id}")
def application(application_id: str, request: Request):
    return application_service(request).admin_resource(application_id, current_actor(request, True))


@router.get("/application-publications")
def application_publications(request: Request):
    return application_service(request).publications(current_actor(request, True), admin=True)


@router.get("/application-publications/{publication_id}")
def application_publication(publication_id: str, request: Request):
    return application_service(request).publication(
        publication_id, current_actor(request, True), admin=True
    )


# 接收管理端审核 POST：路径带申请编号，JSON 正文带审核结论、原因和修订号。
# 同一后端实例的用户端和管理端路由共用业务对象；用户端重新查询可读取处理结果。
@router.post("/application-publications/{publication_id}/review")
def review_application(
    publication_id: str,
    payload: ReviewApplicationPublication,
    request: Request,
):
    return application_service(request).review(
        publication_id, payload, current_actor(request, True)
    )


@router.put("/applications/{application_id}/visibility")
def application_visibility(
    application_id: str,
    payload: ApplicationVisibility,
    request: Request,
):
    return application_service(request).visibility(
        application_id, payload, current_actor(request, True)
    )


@router.get("/application-review-queue")
def application_review_queue(request: Request, query: Annotated[ApplicationReviewQuery, Query()]):
    return query_publications(
        application_service(request).publications(current_actor(request, True), admin=True), query
    )


@router.get("/application-configuration")
def application_configuration(request: Request):
    require_admin(current_actor(request, True))
    return application_service(request).configuration()


@router.put("/application-configuration")
def save_application_configuration(payload: SaveApplicationConfiguration, request: Request):
    if current_actor(request, True).role != "admin":
        raise HTTPException(403, "只有系统管理员可以修改表单配置")
    return application_service(request).configuration(payload)
