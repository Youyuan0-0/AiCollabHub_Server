"""个人中心聚合各业务模块真实状态，身份资料由 IAM 维护。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from app.modules.applications.application_service import application_service
from app.modules.common.fixtures import paginate
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import configured_directory, publication_defaults
from app.modules.common.schemas import CatalogQuery, PageQuery
from app.modules.datasets.workspace import dataset_workspace
from app.modules.knowledge.service import knowledge_service
from app.modules.models.workspace import model_workspace
from app.modules.tools.workspace import tool_workspace

router = APIRouter(prefix="/profile", tags=["personal center"])


class ProfileQuery(PageQuery):
    """个人列表按资源类型、业务状态和关键词过滤。"""

    kind: Literal["", "dataset", "model", "application", "knowledge", "tool"] = ""
    status: str = ""


@router.get("")
def profile(request: Request) -> dict:
    """身份取可信来源；尚未同步的工号和部门留空，不编造真实资料。"""
    actor = current_actor(request)
    identity = publication_defaults(request, actor)
    directory = configured_directory(request)
    member = (
        next((member for member in directory.members if member.id == actor.id), None)
        if directory
        else None
    )
    trusted = member.model_dump(by_alias=True) if member else {}
    if request.app.state.settings.auth_mode == "disabled":
        trusted = {"employeeNumber": f"DEMO-{actor.id}", "department": "模拟部门"}
    return {
        "name": actor.name,
        "employeeNumber": trusted.get("employeeNumber", ""),
        "organization": identity["owner"],
        "department": trusted.get("department", ""),
        "role": actor.role,
        "contact": trusted.get("contact", ""),
    }


@router.get("/publications")
def publications(request: Request, query: Annotated[ProfileQuery, Query()]) -> dict:
    """从五类发布源聚合状态，不以固定文案伪造已发布。"""
    actor = current_actor(request)
    loaders = {
        "dataset": lambda: dataset_workspace(request).publications(actor),
        "model": lambda: model_workspace(request).publications(actor),
        "application": lambda: [
            item
            for item in application_service(request).publications(actor)
            if item["createdBy"] == actor.id
        ],
        # 个人中心与知识库页面共用本地审批记录，避免显示另一套工具链工作区的数据。
        "knowledge": lambda: knowledge_service(request).publications(actor),
        "tool": lambda: tool_workspace(request).publications(actor),
    }
    items = []
    for kind, loader in loaders.items():
        if query.kind and query.kind != kind:
            continue
        for item in loader():
            if (query.status and query.status != item["status"]) or query.q.casefold() not in item[
                "data"
            ]["name"].casefold():
                continue
            path = {
                "dataset": "datasets",
                "model": "models",
                "application": "applications",
                "tool": "tools",
            }.get(kind)
            href = (
                f"/{path}/publications/{item['id']}"
                if path
                else f"/knowledge?resourceId={item['resourceId']}"
            )
            items.append(
                {
                    "id": item["id"],
                    "kind": kind,
                    "name": item["data"]["name"],
                    "status": item["status"],
                    "version": item["data"].get("version", ""),
                    "updatedAt": item["updatedAt"],
                    "href": href,
                }
            )
    return paginate(
        sorted(items, key=lambda item: item["updatedAt"], reverse=True), query.page, query.page_size
    )


@router.get("/favorites")
def favorites(request: Request, query: Annotated[ProfileQuery, Query()]) -> dict:
    """聚合四类仍有访问权限的收藏，取消收藏继续调用各自业务接口。"""
    actor = current_actor(request)
    items = []
    if query.kind in ("", "dataset"):
        workspace = dataset_workspace(request)
        ids = workspace.activity(actor)["favoriteIds"]
        items.extend(
            {**item, "kind": "dataset"} for item in workspace.resources(actor) if item["id"] in ids
        )
    if query.kind in ("", "model"):
        workspace = model_workspace(request)
        items.extend(
            {**item, "kind": "model"} for item in workspace.catalog(actor) if item.get("isFavorite")
        )
    if query.kind in ("", "application"):
        service = application_service(request)
        ids = service.workflows.activity(actor)["favoriteIds"]
        items.extend(
            {**service.resource(identifier, actor), "kind": "application"} for identifier in ids
        )
    if query.kind in ("", "tool"):
        workspace = tool_workspace(request)
        page = 1
        while True:
            result = workspace.catalog(
                CatalogQuery(page=page, page_size=100), actor, favorites_only=True
            )
            items.extend({**item, "kind": "tool"} for item in result["items"])
            if page * 100 >= result["total"]:
                break
            page += 1
    paths = {
        "dataset": "datasets",
        "model": "models",
        "application": "applications",
        "tool": "tools",
    }
    values = [
        {
            "id": item["id"],
            "kind": item["kind"],
            "name": item["name"],
            "version": item.get("version", ""),
            "updatedAt": item.get("updatedAt", ""),
            "href": f"/{paths[item['kind']]}?resourceId={item['id']}",
        }
        for item in items
        if query.q.casefold() in item["name"].casefold()
    ]
    return paginate(values, query.page, query.page_size)
