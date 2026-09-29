"""个人中心聚合各业务模块真实状态，身份资料由 IAM 维护。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from app.modules.applications.application_service import application_service
from app.modules.applications.workflow_schemas import InteractionInput
from app.modules.common.fixtures import paginate
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import configured_directory, publication_defaults
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.workspace import dataset_workspace
from app.modules.knowledge.service import knowledge_service
from app.modules.models.workspace import model_workspace
from app.modules.profile.messages import (
    ProfileMessagesService,
    mark_repository_messages_read,
    message_view,
)
from app.modules.profile.repository import ProfilePublicationsRepository
from app.modules.profile.resources import ProfileResourcesRepository
from app.modules.profile.schemas import ProfileQuery, ReadMessages
from app.modules.tools.workspace import tool_workspace

router = APIRouter(prefix="/profile", tags=["personal center"])


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
    """数据库模式读取本人申请的轻量分页；隔离演示沿用各模块内存仓储。"""
    actor = current_actor(request)
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is not None:
        return ProfilePublicationsRepository(foundation).list_for_user(actor.id, query)
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
    """聚合四类仍有访问权限的收藏，数据库模式直接查询本人关系并分页。"""
    actor = current_actor(request)
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is not None:
        return ProfileResourcesRepository(foundation).list_for_user(actor, "favorites", query)
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


@router.get("/subscriptions")
def subscriptions(request: Request, query: Annotated[ProfileQuery, Query()]) -> dict:
    """订阅列表只读取现有场景应用订阅，不加载运行记录和收藏信息。"""
    actor = current_actor(request)
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is not None:
        return ProfileResourcesRepository(foundation).list_for_user(actor, "subscriptions", query)
    items = [
        {
            "id": item["id"],
            "kind": "application",
            "name": item["name"],
            "version": item.get("version", ""),
            "updatedAt": item["updatedAt"],
            "description": item["description"],
            "owner": item["owner"],
            "href": f"/applications?resourceId={item['id']}",
        }
        for item in application_service(request).workflows.activity(actor)["subscriptions"]
        if query.kind in ("", "application")
        and query.q.casefold() in f"{item['name']} {item.get('description', '')}".casefold()
        and item.get("listingStatus", "listed") == "listed"
    ]
    return paginate(items, query.page, query.page_size)


def profile_messages(request: Request) -> ProfileMessagesService | None:
    """为个人中心装配现有通知存储，不建立另一套消息来源。"""
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is None:
        return None
    return ProfileMessagesService(
        ProfileResourcesRepository(foundation),
        application_service(request).repository.runtime,
    )


@router.get("/messages")
def messages(request: Request) -> list[dict]:
    """提供本人消息和未读角标所需资料，不随其他栏目查询全量应用活动。"""
    actor = current_actor(request)
    service = profile_messages(request)
    if service is not None:
        return service.list_for_user(actor)
    return [
        message_view(item)
        for item in application_service(request).workflows.activity(actor)["notifications"]
        if item["recipient"] == actor.id and item["kind"] in {"review", "new_version"}
    ]


@router.put("/messages/read")
def read_messages(payload: ReadMessages, request: Request) -> dict:
    """批量标记本人消息；失败不能返回成功或把其他用户消息标记为已读。"""
    actor = current_actor(request)
    service = profile_messages(request)
    if service is not None:
        return {"readIds": service.mark_read(actor, payload.ids)}
    return {
        "readIds": mark_repository_messages_read(
            application_service(request).repository, actor, payload.ids
        )
    }


@router.delete("/favorites/{kind}/{resource_id}")
def remove_favorite(
    kind: Literal["dataset", "model", "application", "tool"],
    resource_id: Annotated[str, Path(min_length=1, max_length=200)],
    request: Request,
) -> dict:
    """仅取消本人的收藏，重复取消成功且不会修改订阅、评分或资源资料。"""
    actor = current_actor(request)
    foundation = getattr(request.app.state, "foundation", None)
    key = str(resource_id)
    if foundation is not None:
        ProfileResourcesRepository(foundation).remove_relation(actor, "favorites", kind, key)
    elif kind == "dataset":
        dataset_workspace(request).interact(key, actor, favorite=False)
    elif kind == "model":
        model_workspace(request).interact(key, actor, favorite=False)
    elif kind == "tool":
        tool_workspace(request).interact(key, actor, favorite=False)
    else:
        application_service(request).workflows.interaction(
            key, actor, InteractionInput(favorite=False)
        )
    return {"favorite": False}


@router.delete("/subscriptions/{resource_id}")
def remove_subscription(
    resource_id: Annotated[str, Path(min_length=1, max_length=200)],
    request: Request,
) -> dict:
    """仅取消本人的场景应用订阅，不取消同一资源的收藏。"""
    actor = current_actor(request)
    foundation = getattr(request.app.state, "foundation", None)
    if foundation is not None:
        ProfileResourcesRepository(foundation).remove_relation(
            actor,
            "subscriptions",
            "application",
            str(resource_id),
        )
    else:
        application_service(request).workflows.interaction(
            str(resource_id),
            actor,
            InteractionInput(subscribed=False),
        )
    return {"subscribed": False}
