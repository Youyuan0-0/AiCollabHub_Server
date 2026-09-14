from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import ACTORS, DIRECTORY, current_actor, require_admin
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

router = APIRouter(tags=["application workflows (development)"])


def session_view(request: Request, admin=False):
    authenticated = request.app.state.settings.auth_mode != "disabled"
    try:
        current = current_actor(request, admin).public()
    except HTTPException as error:
        if authenticated or error.status_code != 401:
            raise
        # 重启清空演示会话后仍提供选择器，业务接口继续拒绝旧令牌。
        current = None
    return {
        "current": current,
        "actors": [] if authenticated else [item.public() for item in ACTORS],
        "directory": {"organizations": [], "members": [], "projects": []}
        if authenticated
        else DIRECTORY,
    }


@router.get("/application-session")
def user_session(request: Request):
    return session_view(request)


@router.get("/admin/application-session")
def admin_session(request: Request):
    # 使用 /admin 前缀，使公共登录依赖检查管理端 Cookie。
    return session_view(request, admin=True)


@router.get("/application-demo-session")
def demo_session(request: Request, audience: str = "user"):
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


@router.post("/application-demo-session")
def select_session(payload: DemoSessionInput, request: Request, response: Response):
    if request.app.state.settings.auth_mode != "disabled":
        raise HTTPException(404, "演示账号切换仅在关闭登录时提供")
    token, actor = request.app.state.application_identity.select(payload.actor_id)
    response.set_cookie(
        f"application_{payload.audience}", token, httponly=True, samesite="strict", path="/api/v1"
    )
    return actor.public()


@router.get("/application-presentation")
def presentation(request: Request):
    return application_service(request).workflows.policy()


@router.get("/applications/{application_id}/interaction")
def interaction(application_id: str, request: Request):
    return application_service(request).workflows.interaction(
        application_id, current_actor(request)
    )


@router.put("/applications/{application_id}/interaction")
def save_interaction(application_id: str, payload: InteractionInput, request: Request):
    return application_service(request).workflows.interaction(
        application_id, current_actor(request), payload
    )


@router.get("/applications/{application_id}/ratings")
def ratings(application_id: str, request: Request):
    return application_service(request).workflows.ratings(application_id, current_actor(request))


@router.post("/applications/{application_id}/runs")
def run(application_id: str, payload: RunInput, request: Request):
    return application_service(request).workflows.run(
        application_id, current_actor(request), payload
    )


class DownloadInput(ApplicationPayload):
    request_id: str = Field(min_length=8, max_length=100)


@router.post("/applications/{application_id}/downloads")
def download(application_id: str, payload: DownloadInput, request: Request):
    return application_service(request).workflows.download(
        application_id, current_actor(request), payload.request_id
    )


@router.get("/application-activity")
def activity(request: Request):
    return application_service(request).workflows.activity(current_actor(request))


@router.put("/application-notifications/{notification_id}/read")
def read_notification(notification_id: str, request: Request):
    return application_service(request).workflows.read_notification(
        notification_id, current_actor(request)
    )


@router.get("/application-toolchain/resources")
def toolchain(request: Request):
    current_actor(request)
    return application_service(request).provider.toolchain_resources()


@router.get("/application-toolchain/syncs")
def syncs(request: Request):
    return application_service(request).workflows.syncs(current_actor(request))


@router.post("/application-toolchain/syncs")
def sync(payload: SyncInput, request: Request):
    return application_service(request).workflows.sync(current_actor(request), payload)


@router.get("/admin/application-policy")
def policy(request: Request):
    return application_service(request).workflows.policy(current_actor(request, True))


@router.get("/admin/application-dependencies")
def admin_dependencies(request: Request):
    require_admin(current_actor(request, True))
    return application_service(request).provider.dependencies()


@router.put("/admin/application-policy")
def save_policy(payload: WorkflowPolicy, request: Request):
    return application_service(request).workflows.policy(current_actor(request, True), payload)


@router.get("/admin/application-publications/{publication_id}/quality")
def quality(publication_id: str, request: Request):
    service = application_service(request)
    actor = current_actor(request, True)
    record = service.publication(publication_id, actor, admin=True)
    return service.workflows.quality(record, actor)


@router.get("/admin/application-review-history")
def history(request: Request, query: Annotated[HistoryQuery, Query()]):
    return application_service(request).workflows.history(current_actor(request, True), query)


@router.get("/admin/application-syncs")
def admin_syncs(request: Request):
    return application_service(request).workflows.syncs(current_actor(request, True), admin=True)


@router.get("/admin/application-notifications")
def admin_notifications(request: Request):
    actor = current_actor(request, True)
    require_admin(actor)
    return application_service(request).workflows.activity(actor)["notifications"]


@router.post("/application-toolchain/development-publications")
def develop(payload: SaveApplicationPublication, request: Request):
    actor = current_actor(request)
    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前演示账号没有工具链发布权限")
    from app.modules.applications.configuration import validate_submission

    svc = application_service(request)
    data = payload.data.model_dump(by_alias=True)
    with svc.repository.transaction() as session:
        validate_submission(data, session.get_configuration())
        svc.workflows.validate_security(session, data, payload.security_level)
    svc.provider.validate_dependencies(data["dependencies"])
    return svc.provider.develop(data, actor.id)
