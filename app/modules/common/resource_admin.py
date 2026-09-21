"""模型和知识库共用的管理流程，字段配置和完整性校验由业务模块注入。"""

from collections.abc import Callable
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.common.fixtures import paginate
from app.modules.common.identity import Actor, current_actor, require_admin
from app.modules.common.memory_reports import report_router, reports_store
from app.modules.common.resource_workspace import (
    ResourceReview,
    ResourceVisibility,
    ResourceWorkspace,
)
from app.modules.common.schemas import PageQuery
from app.modules.common.visibility import validate_visibility_targets, visibility_directory


class AdminQuery(PageQuery):
    """管理列表保留资源独立状态，日期按 UTC 天过滤。"""

    status: str = ""
    reviewer: str = ""
    start: date | None = None
    end: date | None = None


def resource_admin_router(
    kind: str,
    plural: str,
    workspace: Callable[[Request], ResourceWorkspace],
    fields: dict[str, str],
    required: tuple[str, ...],
    validate: Callable[[dict], None],
) -> APIRouter:
    """复用目录、审核、版本和范围交互，不在公共层假定业务字段相同。"""
    router = APIRouter(prefix="/admin", tags=[f"{kind} administration"])

    def get_record(request: Request, identifier: str, actor: Actor) -> dict:
        """按业务仓储校验审核资料读取权限。"""
        return workspace(request).publication(identifier, actor, True)

    @router.get(f"/{kind}-configuration")
    def configuration(request: Request) -> dict:
        """展示依据任务表固定的字段规则，不允许删除核心要求。"""
        require_admin(current_actor(request, True))
        return {
            "fields": [
                {"key": key, "label": label, "required": key in required}
                for key, label in fields.items()
            ]
        }

    @router.get(f"/{plural}")
    def resources(request: Request, query: Annotated[AdminQuery, Query()]) -> dict:
        """分页列出当前管理员可管理资源。"""
        items = workspace(request).catalog(current_actor(request, True), True)
        items = [
            item
            for item in items
            if query.q.casefold() in (item["name"] + item.get("summary", "")).casefold()
        ]
        return paginate(items, query.page, query.page_size)

    @router.get(f"/{plural}/{{identifier}}")
    def detail(identifier: str, request: Request) -> dict:
        """返回管理元数据、历史及范围目录。"""
        actor = current_actor(request, True)
        return {
            **workspace(request).resource(identifier, actor, True),
            "visibilityDirectory": visibility_directory(request, actor),
        }

    @router.put(f"/{plural}/{{identifier}}/visibility")
    def visibility(identifier: str, payload: ResourceVisibility, request: Request) -> dict:
        """保存独立资源权限，同时检查当前管理员可选择的目标。"""
        actor = current_actor(request, True)
        validate_visibility_targets(request, actor, payload)
        return workspace(request).set_visibility(identifier, payload, actor)

    @router.get(f"/{kind}-publications")
    def queue(request: Request, query: Annotated[AdminQuery, Query()]) -> dict:
        """按状态、关键词和提交日期过滤审核队列。"""
        if query.start and query.end and query.start > query.end:
            raise HTTPException(422, "开始日期不能晚于结束日期")
        items = workspace(request).publications(current_actor(request, True), True)
        items = [
            item
            for item in items
            if (not query.status or item["status"] == query.status)
            and query.q.casefold() in item["data"]["name"].casefold()
            and (not query.start or (item.get("submittedAt") or "")[:10] >= query.start.isoformat())
            and (not query.end or (item.get("submittedAt") or "")[:10] <= query.end.isoformat())
        ]
        items.sort(key=lambda item: item.get("submittedAt") or item["createdAt"], reverse=True)
        return paginate(items, query.page, query.page_size)

    @router.get(f"/{kind}-publications/{{identifier}}")
    def publication(identifier: str, request: Request) -> dict:
        """取得管理权限内的完整审核资料。"""
        return get_record(request, identifier, current_actor(request, True))

    @router.post(f"/{kind}-publications/{{identifier}}/review")
    def review(identifier: str, payload: ResourceReview, request: Request) -> dict:
        """评分与报告共用基础协议，批准前执行资源专属验证。"""
        actor = current_actor(request, True)
        store = reports_store(request)
        with store.lock:
            record = get_record(request, identifier, actor)
            if payload.decision == "approved":
                validate(record["data"])
            reports = store.selected(kind, record, actor, payload.report_file_ids)
            return workspace(request).review(identifier, payload, actor, reports)

    @router.get(f"/{kind}-audit")
    def audit(request: Request, query: Annotated[AdminQuery, Query()]) -> dict:
        """按审核员、日期与结论查询历史快照。"""
        if query.start and query.end and query.start > query.end:
            raise HTTPException(422, "开始日期不能晚于结束日期")
        records = workspace(request).publications(current_actor(request, True), True)
        items = [
            {**review, "publicationId": record["id"]}
            for record in records
            for review in record["reviews"]
        ]
        items = [
            item
            for item in items
            if (not query.reviewer or query.reviewer in item["reviewerName"])
            and (not query.status or query.status == item["decision"])
            and query.q.casefold() in item["data"]["name"].casefold()
            and (not query.start or item["reviewedAt"][:10] >= query.start.isoformat())
            and (not query.end or item["reviewedAt"][:10] <= query.end.isoformat())
        ]
        return paginate(
            sorted(items, key=lambda item: item["reviewedAt"], reverse=True),
            query.page,
            query.page_size,
        )

    router.include_router(report_router(kind, get_record, prefix=""))
    return router
