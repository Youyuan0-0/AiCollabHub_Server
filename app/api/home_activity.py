"""首页动态的管理员接口；公告和运营设置仅写入本地进程内存。"""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.db.foundation import FoundationDatabase
from app.modules.common.identity import Actor, current_actor
from app.services.home_activity import HomeActivityStore, activity_records

router = APIRouter(prefix="/admin/home-activity", tags=["home activity"])
ActivityKind = Literal["announcement", "dataset", "model", "application", "knowledge", "tool"]
ResourceKind = Literal["dataset", "model", "application", "knowledge", "tool"]


class ActivityInput(BaseModel):
    """公告编辑和运营控制统一使用前端 camelCase 字段。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class AnnouncementInput(ActivityInput):
    """公告草稿可缺正文；正式发布和撤回时必须保留正文。"""

    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=500)
    content_markdown: str = Field(default="", max_length=20000)
    status: Literal["draft", "published", "withdrawn"]
    is_pinned: bool = False


class ActivityControlInput(ActivityInput):
    """资源动态的隐藏与置顶状态，公告通过编辑自身状态管理。"""

    hidden: bool
    is_pinned: bool


class ActivitySettingInput(ActivityInput):
    """指定来源是否参与首页动态；关闭不会删除历史内容。"""

    enabled: bool


def activity_context(
    request: Request,
) -> tuple[HomeActivityStore, FoundationDatabase | None, Actor]:
    """使用已验证的管理身份获取当前应用实例的内存仓库。"""
    actor = current_actor(request, True)
    if actor.role != "admin":
        raise HTTPException(403, "只有系统管理员可以管理首页动态")
    return request.app.state.home_activity, getattr(request.app.state, "foundation", None), actor


def announcement_value(payload: AnnouncementInput) -> dict:
    """校验公告有效文本并转换为内存仓库使用的字段。"""
    if not payload.title.strip():
        raise HTTPException(422, "公告标题不能为空")
    if payload.status != "draft" and not payload.content_markdown.strip():
        raise HTTPException(422, "发布或撤回的公告必须有正文")
    return {
        "title": payload.title.strip(),
        "summary": payload.summary.strip(),
        "contentMarkdown": payload.content_markdown.strip(),
        "status": payload.status,
        "isPinned": payload.is_pinned,
    }


@router.get("")
def list_activity(request: Request) -> dict:
    """返回本地公告、来源开关及当前可管理的正式资源动态。"""
    store, foundation, actor = activity_context(request)
    announcements, settings, _ = store.snapshot()
    announcements.sort(key=lambda item: item["createdAt"], reverse=True)
    return {
        "announcements": announcements[:200],
        "events": activity_records(foundation, actor, store, include_hidden=True),
        "settings": settings,
    }


@router.post("/announcements", status_code=201)
def create_announcement(payload: AnnouncementInput, request: Request) -> dict:
    """新建草稿或直接发布；禁止新公告直接进入撤回状态。"""
    store, _, _ = activity_context(request)
    if payload.status == "withdrawn":
        raise HTTPException(422, "新公告不能直接撤回")
    return {"id": store.create_announcement(announcement_value(payload))}


@router.put("/announcements/{announcement_id}")
def save_announcement(announcement_id: UUID, payload: AnnouncementInput, request: Request) -> dict:
    """保存公告编辑与状态变更；不存在的编号返回 404。"""
    store, _, _ = activity_context(request)
    if not store.save_announcement(str(announcement_id), announcement_value(payload)):
        raise HTTPException(404, "公告不存在")
    return {"id": str(announcement_id)}


@router.put("/settings/{kind}")
def save_setting(kind: ActivityKind, payload: ActivitySettingInput, request: Request) -> dict:
    """调整某类动态的展示开关，立即影响当前实例的首页响应。"""
    store, _, _ = activity_context(request)
    store.set_setting(kind, payload.enabled)
    return {"kind": kind, "enabled": payload.enabled}


@router.put("/events/{kind}/{version_id}")
def save_control(
    kind: ResourceKind, version_id: UUID, payload: ActivityControlInput, request: Request
) -> dict:
    """只允许控制当前上架且具有正式版本的资源动态。"""
    store, foundation, actor = activity_context(request)
    if not any(
        item["kind"] == kind and item["id"] == str(version_id)
        for item in activity_records(foundation, actor, store, include_hidden=True)
    ):
        raise HTTPException(404, "动态不存在或资源已下架")
    store.set_control(kind, str(version_id), hidden=payload.hidden, is_pinned=payload.is_pinned)
    return {"kind": kind, "id": str(version_id)}
