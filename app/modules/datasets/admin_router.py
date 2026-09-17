"""数据集管理路由，统一复用管理端认证、CSRF 及可信身份。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.modules.applications.identity import can_manage, current_actor
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.admin_schemas import (
    DatasetAuditQuery,
    DatasetReviewQuery,
    DatasetVisibility,
    PublishDataset,
    ReviewDataset,
)
from app.modules.datasets.admin_service import DatasetAdminService, require_dataset_admin
from app.modules.datasets.visibility import validate_visibility_targets, visibility_directory
from app.modules.datasets.workspace import dataset_workspace

router = APIRouter(prefix="/admin", tags=["dataset administration (development)"])


@router.get("/datasets", response_model=None)
def datasets(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """读取公开样例目录及真实活动统计，不把未知组织标签当作管理授权。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    return {
        **dataset_workspace(request).catalog(query, actor),
        "source": "fixture",
        "downloadScope": "sample",
    }


@router.get("/datasets/{dataset_id}", response_model=None)
def dataset(dataset_id: str, request: Request) -> dict:
    """返回详情、当前范围和可选目录；编辑权限由可信资源归属决定。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    workspace = dataset_workspace(request)
    resource = workspace.resource(dataset_id, actor)
    # 展示和保存共用可信目录；正式目录未接入时不补出虚假的组织或成员。
    directory = visibility_directory(request, actor)
    return {
        **resource,
        "visibility": workspace.visibility(dataset_id),
        "visibilityDirectory": directory or {"organizations": [], "members": [], "projects": []},
        "canManageVisibility": can_manage(actor, resource),
    }


@router.put("/datasets/{dataset_id}/visibility", response_model=None)
def visibility(dataset_id: str, payload: DatasetVisibility, request: Request) -> dict:
    """保存开放范围；复用会话、CSRF 和可信目录校验，旧修订不能覆盖新配置。"""
    actor = current_actor(request, is_admin=True)
    require_dataset_admin(actor)
    validate_visibility_targets(request, actor, payload)
    return DatasetAdminService(dataset_workspace(request)).visibility(dataset_id, payload, actor)


@router.get("/dataset-publications", response_model=None)
def publications(request: Request, query: Annotated[DatasetReviewQuery, Query()]) -> dict:
    """按管理员授权范围查询队列，统计不包含其他组织的申请。"""
    return DatasetAdminService(dataset_workspace(request)).queue(
        query, current_actor(request, is_admin=True)
    )


@router.get("/dataset-publications/{publication_id}", response_model=None)
def publication(publication_id: str, request: Request) -> dict:
    """返回待审资料、历史审核快照及当前同步状态。"""
    return DatasetAdminService(dataset_workspace(request)).publication(
        publication_id, current_actor(request, is_admin=True)
    )


@router.post("/dataset-publications/{publication_id}/review", response_model=None)
def review(publication_id: str, payload: ReviewDataset, request: Request) -> dict:
    """提交通过、驳回或需补充结论，服务端检查状态、权限及修订号。"""
    return DatasetAdminService(dataset_workspace(request)).review(
        publication_id, payload, current_actor(request, is_admin=True)
    )


@router.get("/dataset-audit", response_model=None)
def audit(request: Request, query: Annotated[DatasetAuditQuery, Query()]) -> dict:
    """分页返回本管理范围内的历史审核记录。"""
    return DatasetAdminService(dataset_workspace(request)).audit(
        query, current_actor(request, is_admin=True)
    )


@router.post("/dataset-publications/{publication_id}/publish", response_model=None)
def publish(publication_id: str, payload: PublishDataset, request: Request) -> dict:
    """发布已通过但尚未上架的申请，沿用管理角色、组织范围与 CSRF 校验。"""
    return DatasetAdminService(dataset_workspace(request)).publish(
        publication_id, payload.revision, current_actor(request, is_admin=True)
    )
