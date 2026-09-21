"""工具管理复用公共身份、审核、分页及可见目标校验。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.modules.common.fixtures import paginate
from app.modules.common.identity import current_actor, require_admin
from app.modules.common.schemas import CatalogQuery
from app.modules.common.visibility import validate_visibility_targets, visibility_directory
from app.modules.tools.schemas import (
    ReviewTool,
    ToolAuditQuery,
    ToolListing,
    ToolPriority,
    ToolReviewQuery,
    ToolVisibility,
)
from app.modules.tools.workspace import tool_workspace

router = APIRouter(prefix="/admin", tags=["tool administration (simulation)"])


@router.get("/tools", response_model=None)
def resources(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """读取管理员授权范围内目录，包括下架工具。"""
    return tool_workspace(request).catalog(query, current_actor(request, True), True)


@router.get("/tools/{tool_id}", response_model=None)
def detail(tool_id: str, request: Request) -> dict:
    """返回管理资料和可选择的开放目标。"""
    actor = current_actor(request, True)
    return {
        **tool_workspace(request).resource(tool_id, actor, True),
        "visibilityDirectory": visibility_directory(request, actor),
    }


@router.put("/tools/{tool_id}/listing", response_model=None)
def listing(tool_id: str, payload: ToolListing, request: Request) -> dict:
    """更新工具上下架状态。"""
    return tool_workspace(request).listing(tool_id, payload, current_actor(request, True))


@router.put("/tools/{tool_id}/visibility", response_model=None)
def visibility(tool_id: str, payload: ToolVisibility, request: Request) -> dict:
    """校验目录和管理权限后保存开放范围。"""
    actor = current_actor(request, True)
    require_admin(actor)
    validate_visibility_targets(request, actor, payload)
    return tool_workspace(request).visibility(tool_id, payload, actor)


@router.get("/tool-publications", response_model=None)
def publications(request: Request, query: Annotated[ToolReviewQuery, Query()]) -> dict:
    """分页检索管理员可见申请及审核状态。"""
    records = tool_workspace(request).publications(current_actor(request, True), True)
    records = [
        item
        for item in records
        if (not query.status or item["status"] == query.status)
        and query.q.casefold() in (item["data"]["name"] + item["owner"]).casefold()
        and (
            not query.start
            or (item.get("submittedAt") or item["createdAt"])[:10] >= query.start.isoformat()
        )
        and (
            not query.end
            or (item.get("submittedAt") or item["createdAt"])[:10] <= query.end.isoformat()
        )
    ]
    records.sort(key=lambda item: item.get("submittedAt") or item["createdAt"], reverse=True)
    if query.sort == "priority":
        records.sort(
            key=lambda item: {"normal": 0, "high": 1, "urgent": 2}[item["priority"]], reverse=True
        )
    elif query.sort == "updated":
        records.sort(key=lambda item: item["updatedAt"], reverse=True)
    return paginate(records, query.page, query.page_size)


@router.get("/tool-publications/{record_id}", response_model=None)
def publication(record_id: str, request: Request) -> dict:
    """读取申请完整资料及审核快照。"""
    return tool_workspace(request).publication(record_id, current_actor(request, True), True)


@router.post("/tool-publications/{record_id}/review", response_model=None)
def review(record_id: str, payload: ReviewTool, request: Request) -> dict:
    """完成审核并原子发布通过的工具。"""
    return tool_workspace(request).review(record_id, payload, current_actor(request, True))


@router.get("/tool-audit", response_model=None)
def audit(request: Request, query: Annotated[ToolAuditQuery, Query()]) -> dict:
    """分页查询审核及资源管理留痕。"""
    return tool_workspace(request).audit(query, current_actor(request, True))


@router.get("/tool-configuration", response_model=None)
def configuration(request: Request) -> dict:
    """读取工具字段规则与分类选项，不提供额外的字段必填性编辑。"""
    actor = current_actor(request, True)
    require_admin(actor)
    return tool_workspace(request).configuration()


@router.put("/tool-publications/{record_id}/priority", response_model=None)
def prioritize(record_id: str, payload: ToolPriority, request: Request) -> dict:
    """管理员调整审核优先级。"""
    return tool_workspace(request).prioritize(record_id, payload, current_actor(request, True))
