from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field

from app.modules.applications.application_service import application_service
from app.modules.applications.publication_schemas import (
    ApplicationPayload,
    SaveApplicationPublication,
)
from app.modules.applications.workflow_schemas import (
    DemoSessionInput,
    HistoryQuery,
    InteractionInput,
    RunInput,
    SyncInput,
    WorkflowPolicy,
)
from app.modules.common.identity import ACTORS, DIRECTORY, current_actor, require_admin
from app.modules.common.identity_directory import visible_directory
from app.modules.common.toolchain.configuration import module_settings

router = APIRouter(tags=["application workflows (development)"])


def session_view(request: Request, is_admin: bool = False) -> dict:
    """生成当前入口的场景身份视图，正式认证模式不提供演示账号。"""
    authenticated = request.app.state.settings.auth_mode != "disabled"
    try:
        actor = current_actor(request, is_admin)
        current = actor.public()
    except HTTPException as error:
        if authenticated or error.status_code != 401:
            raise
        # 重启清空演示会话后仍提供选择器，业务接口继续拒绝旧令牌。
        current = None
    return {
        "current": current,
        "actors": [] if authenticated else [item.public() for item in ACTORS],
        "directory": visible_directory(request, actor) if authenticated else DIRECTORY,
    }


@router.get("/application-session", response_model=None)
def user_session(request: Request) -> dict:
    """读取用户端场景身份与可见目录。"""
    return session_view(request)


@router.get("/admin/application-session", response_model=None)
def admin_session(request: Request) -> dict:
    """读取管理端场景身份，由管理路由前缀触发管理会话校验。"""
    # 使用 /admin 前缀，使公共登录依赖检查管理端 Cookie。
    return session_view(request, is_admin=True)


@router.get("/application-demo-session", response_model=None)
def demo_session(request: Request, audience: str = "user") -> dict:
    """读取演示身份选择信息，仅在关闭正式认证时开放。"""
    if request.app.state.settings.auth_mode != "disabled":
        raise HTTPException(404, "演示账号切换仅在关闭登录时提供")
    provider = request.app.state.application_identity
    try:
        actor = provider.resolve(request, "admin" if audience == "admin" else "user")
        current = actor.public()
    except Exception:
        current = None
    return {
        "current": current,
        "actors": [actor.public() for actor in ACTORS],
        "directory": DIRECTORY,
    }


@router.post("/application-demo-session", response_model=None)
def select_session(payload: DemoSessionInput, request: Request, response: Response) -> dict:
    """选择预置演示身份并写入该入口的 HttpOnly Cookie。"""
    if request.app.state.settings.auth_mode != "disabled":
        raise HTTPException(404, "演示账号切换仅在关闭登录时提供")
    token, actor = request.app.state.application_identity.select(payload.actor_id)
    response.set_cookie(
        f"application_{payload.audience}", token, httponly=True, samesite="strict", path="/api/v1"
    )
    return actor.public()


@router.get("/application-presentation", response_model=None)
def presentation(request: Request) -> dict:
    """读取应用目录公开的展示策略。"""
    result = application_service(request).workflows.policy()
    settings = request.app.state.settings
    result["toolchainMode"] = module_settings(settings, "applications").toolchain_mode
    if settings.toolchain_application_entry_url:
        result["toolchainUrl"] = str(settings.toolchain_application_entry_url)
    return result


@router.get("/applications/{application_id}/interaction", response_model=None)
def interaction(application_id: str, request: Request) -> dict:
    """读取当前用户与指定应用的互动状态。"""
    return application_service(request).workflows.interaction(
        application_id, current_actor(request)
    )


@router.put("/applications/{application_id}/interaction", response_model=None)
def save_interaction(application_id: str, payload: InteractionInput, request: Request) -> dict:
    """更新当前用户的收藏、订阅或评分。"""
    return application_service(request).workflows.interaction(
        application_id, current_actor(request), payload
    )


@router.get("/applications/{application_id}/ratings", response_model=None)
def ratings(application_id: str, request: Request) -> list[dict]:
    """读取可见应用的用户评分列表。"""
    return application_service(request).workflows.ratings(application_id, current_actor(request))


@router.post("/applications/{application_id}/runs", response_model=None)
def run(application_id: str, payload: RunInput, request: Request) -> dict:
    """校验身份后执行应用，返回已记录的运行结果。"""
    return application_service(request).workflows.run(
        application_id, current_actor(request), payload
    )


class DownloadInput(ApplicationPayload):
    """应用下载请求的幂等编号，重试需沿用同一编号。"""

    request_id: str = Field(min_length=8, max_length=100)


@router.post("/applications/{application_id}/downloads", response_model=None)
def download(application_id: str, payload: DownloadInput, request: Request) -> dict:
    """读取应用下载资料并按幂等编号记录下载行为。"""
    return application_service(request).workflows.download(
        application_id, current_actor(request), payload.request_id
    )


@router.get("/application-activity", response_model=None)
def activity(request: Request) -> dict:
    """读取当前身份的应用活动概览。"""
    return application_service(request).workflows.activity(current_actor(request))


@router.put("/application-notifications/{notification_id}/read", response_model=None)
def read_notification(notification_id: str, request: Request) -> dict:
    """将当前用户的指定通知标记为已读。"""
    return application_service(request).workflows.read_notification(
        notification_id, current_actor(request)
    )


@router.get("/application-toolchain/resources", response_model=None)
def toolchain(request: Request) -> list[dict]:
    """读取当前供应方可导入的工具链资源列表。"""
    actor = current_actor(request)
    provider = application_service(request).provider
    if hasattr(provider, "authorize"):
        provider.authorize(actor)
    return provider.toolchain_resources()


@router.get("/application-toolchain/syncs", response_model=None)
def syncs(request: Request) -> list[dict]:
    """读取当前用户的工具链同步记录。"""
    return application_service(request).workflows.syncs(current_actor(request))


@router.post("/application-toolchain/syncs", response_model=None)
def sync(payload: SyncInput, request: Request) -> dict:
    """导入工具链资源的指定版本并记录同步结果。"""
    return application_service(request).workflows.sync(current_actor(request), payload)


@router.get("/admin/application-policy", response_model=None)
def policy(request: Request) -> dict:
    """读取当前管理身份可查看的完整应用策略。"""
    return application_service(request).workflows.policy(current_actor(request, True))


@router.get("/admin/application-dependencies", response_model=None)
def admin_dependencies(request: Request) -> list[dict]:
    """校验管理角色后读取关联资源目录。"""
    actor = current_actor(request, True)
    require_admin(actor)
    return application_service(request).dependencies(actor)


@router.put("/admin/application-policy", response_model=None)
def save_policy(payload: WorkflowPolicy, request: Request) -> dict:
    """按策略版本保存应用规则，由业务服务检查管理权限。"""
    return application_service(request).workflows.policy(current_actor(request, True), payload)


@router.get("/admin/application-publications/{publication_id}/quality", response_model=None)
def quality(publication_id: str, request: Request) -> dict:
    """读取可审核申请的质量检查结果。"""
    service = application_service(request)
    actor = current_actor(request, True)
    record = service.publication(publication_id, actor, is_admin=True)
    return service.workflows.quality(record, actor)


@router.get("/admin/application-review-history", response_model=None)
def history(request: Request, query: Annotated[HistoryQuery, Query()]) -> dict:
    """按筛选条件分页读取有权查看的审核历史。"""
    return application_service(request).workflows.history(current_actor(request, True), query)


@router.get("/admin/application-syncs", response_model=None)
def admin_syncs(request: Request) -> list[dict]:
    """读取当前管理身份有权查看的同步记录。"""
    return application_service(request).workflows.syncs(current_actor(request, True), is_admin=True)


@router.get("/admin/application-notifications", response_model=None)
def admin_notifications(
    request: Request, kind: Literal["review", "new_version", "run_failure"] | None = None
) -> list[dict]:
    """按管理身份和可选通知类型读取消息，运行页不混入审核结果。"""
    actor = current_actor(request, True)
    require_admin(actor)
    notifications = application_service(request).workflows.activity(actor)["notifications"]
    return [item for item in notifications if kind is None or item["kind"] == kind]


@router.post("/application-toolchain/development-publications", response_model=None)
def develop(payload: SaveApplicationPublication, request: Request) -> dict:
    """校验发布资料、关联资源及密级后提交工具链开发资料。"""
    actor = current_actor(request)
    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前演示账号没有工具链发布权限")
    from app.modules.applications.configuration import validate_submission

    service = application_service(request)
    data = payload.data.model_dump(by_alias=True)
    with service.repository.transaction() as session:
        validate_submission(data, session.get_configuration())
        service.workflows.validate_security(session, data, payload.security_level)
        from app.modules.applications.tutorial_files import validate_files

        validate_files(session, data, actor, payload.security_level)
    service.validate_dependencies(data["dependencies"], actor)
    return service.provider.develop(data, actor.id)
