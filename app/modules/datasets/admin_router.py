"""数据集管理路由，统一复用管理端认证、CSRF 及可信身份。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.modules.common.identity import can_manage, current_actor
from app.modules.common.memory_reports import reports_store
from app.modules.common.visibility import validate_visibility_targets, visibility_directory
from app.modules.datasets.admin_schemas import (
    DatasetAdminQuery,
    DatasetAuditQuery,
    DatasetListing,
    DatasetMetadata,
    DatasetReviewQuery,
    DatasetVisibility,
    PublishDataset,
    ReviewDataset,
)
from app.modules.datasets.admin_service import DatasetAdminService, require_dataset_admin
from app.modules.datasets.workspace import dataset_workspace

router = APIRouter(prefix="/admin", tags=["dataset administration (development)"])


@router.get("/datasets", response_model=None)
def datasets(request: Request, query: Annotated[DatasetAdminQuery, Query()]) -> dict:
    """读取已发布目录及下架资料，按状态筛选并返回可信操作权限。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    return {
        **DatasetAdminService(dataset_workspace(request)).catalog(query, actor),
        "source": "fixture",
        "downloadScope": "sample",
    }


@router.get("/datasets/{dataset_id}", response_model=None)
def dataset(dataset_id: str, request: Request) -> dict:
    """返回详情、当前范围和可选目录；编辑权限由可信资源归属决定。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    workspace = dataset_workspace(request)
    resource = workspace.resource(dataset_id, actor, include_unlisted=True)
    # 展示和保存共用可信目录；正式目录未接入时不补出虚假的组织或成员。
    directory = visibility_directory(request, actor)
    return {
        **resource,
        "visibility": workspace.visibility(dataset_id),
        "visibilityDirectory": directory or {"organizations": [], "members": [], "projects": []},
        "canManageVisibility": can_manage(actor, resource),
        "canManageListing": can_manage(actor, resource),
    }


@router.put("/datasets/{dataset_id}/listing", response_model=None)
def listing(dataset_id: str, payload: DatasetListing, request: Request) -> dict:
    """按修订号上架或下架，复用管理会话及 CSRF，过期页面返回 409。"""
    return DatasetAdminService(dataset_workspace(request)).listing(
        dataset_id, payload, current_actor(request, is_admin=True)
    )


@router.put("/datasets/{dataset_id}/visibility", response_model=None)
def visibility(dataset_id: str, payload: DatasetVisibility, request: Request) -> dict:
    """保存开放范围；复用会话、CSRF 和可信目录校验，旧修订不能覆盖新配置。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    validate_visibility_targets(request, actor, payload)
    return DatasetAdminService(dataset_workspace(request)).visibility(dataset_id, payload, actor)


@router.put("/datasets/{dataset_id}/metadata", response_model=None)
def metadata(dataset_id: str, payload: DatasetMetadata, request: Request) -> dict:
    """保存数据血缘、存储位置和 API 地址；资料归属与修订均由服务端校验。"""
    return DatasetAdminService(dataset_workspace(request)).metadata(
        dataset_id, payload, current_actor(request, is_admin=True)
    )


@router.get("/dataset-publications", response_model=None)
def publications(request: Request, query: Annotated[DatasetReviewQuery, Query()]) -> dict:
    """按管理员授权查询队列，返回当前上下架状态而不改写原审核结论。"""
    workspace = dataset_workspace(request)
    page = DatasetAdminService(workspace).queue(query, current_actor(request, is_admin=True))
    return {**page, "items": workspace.publication_listings(page["items"])}


@router.get("/dataset-publications/{publication_id}", response_model=None)
def publication(publication_id: str, request: Request) -> dict:
    """返回资料、审核快照及实时上下架状态，下架不会丢失申请历史。"""
    workspace = dataset_workspace(request)
    record = DatasetAdminService(workspace).publication(
        publication_id, current_actor(request, is_admin=True)
    )
    return workspace.publication_listings([record])[0]


@router.post("/dataset-publications/{publication_id}/review", response_model=None)
def review(publication_id: str, payload: ReviewDataset, request: Request) -> dict:
    """校验权限和修订后审核，响应附带发布结果的当前上下架状态。"""
    workspace = dataset_workspace(request)
    actor = current_actor(request, is_admin=True)
    service = DatasetAdminService(workspace)
    store = reports_store(request)
    with store.lock:
        current = service.publication(publication_id, actor)
        reports = store.selected("dataset", current, actor, payload.report_file_ids)
        record = service.review(publication_id, payload, actor, reports)
    return workspace.publication_listings([record])[0]


@router.get("/dataset-audit", response_model=None)
def audit(request: Request, query: Annotated[DatasetAuditQuery, Query()]) -> dict:
    """分页返回本管理范围内的历史审核记录。"""
    return DatasetAdminService(dataset_workspace(request)).audit(
        query, current_actor(request, is_admin=True)
    )


@router.post("/dataset-publications/{publication_id}/publish", response_model=None)
def publish(publication_id: str, payload: PublishDataset, request: Request) -> dict:
    """补发已通过申请并返回实时状态；既有下架资源不会被补发接口重新上架。"""
    workspace = dataset_workspace(request)
    record = DatasetAdminService(workspace).publish(
        publication_id, payload.revision, current_actor(request, is_admin=True)
    )
    return workspace.publication_listings([record])[0]
