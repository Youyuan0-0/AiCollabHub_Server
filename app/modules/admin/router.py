from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

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


@router.get("/applications", response_model=None)
def applications(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """分页查询当前身份有权管理的应用目录。"""
    warnings: list[str] = []
    items = application_service(request).resources(
        current_actor(request, True), is_admin=True, warnings=warnings
    )
    return {**query_resources(items, query), "warnings": warnings}


@router.get("/applications/{application_id}", response_model=None)
def application(application_id: str, request: Request) -> dict:
    """读取有权管理的应用详情与运行状态。"""
    return application_service(request).admin_resource(application_id, current_actor(request, True))


@router.get("/application-publications", response_model=None)
def application_publications(request: Request) -> list[dict]:
    """读取当前管理身份可查看的发布申请列表。"""
    return application_service(request).publications(current_actor(request, True), is_admin=True)


@router.get("/application-publications/{publication_id}", response_model=None)
def application_publication(publication_id: str, request: Request) -> dict:
    """按申请编号读取有权管理的发布资料和审核记录。"""
    return application_service(request).publication(
        publication_id, current_actor(request, True), is_admin=True
    )


# 接收管理端审核 POST：路径带申请编号，JSON 正文带审核结论、原因和修订号。
# 同一后端实例的用户端和管理端路由共用业务对象；用户端重新查询可读取处理结果。
@router.post("/application-publications/{publication_id}/review", response_model=None)
def review_application(
    publication_id: str,
    payload: ReviewApplicationPublication,
    request: Request,
) -> dict:
    """提交发布审核决定，由业务服务校验权限、版本和质量评分。"""
    return application_service(request).review(
        publication_id, payload, current_actor(request, True)
    )


@router.put("/applications/{application_id}/visibility", response_model=None)
def application_visibility(
    application_id: str,
    payload: ApplicationVisibility,
    request: Request,
) -> dict:
    """保存应用开放范围，由业务服务检查管理权限及并发冲突。"""
    from app.modules.applications.identity_directory import validate_visibility_targets

    actor = current_actor(request, True)
    validate_visibility_targets(request, actor, payload)
    return application_service(request).visibility(application_id, payload, actor)


@router.get("/application-review-queue", response_model=None)
def application_review_queue(
    request: Request, query: Annotated[ApplicationReviewQuery, Query()]
) -> dict:
    """筛选并分页返回审核队列和各状态数量。"""
    return query_publications(
        application_service(request).publications(current_actor(request, True), is_admin=True),
        query,
    )


@router.get("/application-configuration", response_model=None)
def application_configuration(request: Request) -> dict:
    """读取管理端发布表单规则和列表列配置。"""
    require_admin(current_actor(request, True))
    return application_service(request).configuration()


@router.put("/application-configuration", response_model=None)
def save_application_configuration(payload: SaveApplicationConfiguration, request: Request) -> dict:
    """校验管理权限后按版本号保存发布配置。"""
    if current_actor(request, True).role != "admin":
        raise HTTPException(403, "只有系统管理员可以修改表单配置")
    return application_service(request).configuration(payload, actor=current_actor(request, True))
