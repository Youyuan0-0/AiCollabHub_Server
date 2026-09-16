"""真实外部发布入口；复用平台认证、CSRF 与应用归属检查。"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import current_actor
from app.modules.applications.market_contract import MarketMutation, MarketRecovery
from app.modules.applications.market_workflows import MarketWorkflows

router = APIRouter(tags=["application external publishing"])


def market(request):
    # 测试可注入 HTTP transport；正式入口只使用受控服务端配置。
    if hasattr(request.app.state, "application_market"):
        return request.app.state.application_market
    return MarketWorkflows(application_service(request), request.app.state.settings)


@router.get("/application-market")
def status(request: Request):
    return market(request).status(current_actor(request))


@router.get("/admin/application-market")
def admin_status(request: Request):
    return market(request).status(current_actor(request, True), admin=True)


@router.get("/application-market/catalog")
def catalog(
    request: Request,
    keyword: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=12, ge=1, le=100),
):
    actor, service = current_actor(request), market(request)
    if service.settings.applications_market != "himarket":
        raise HTTPException(503, "外部目录服务尚未启用")
    return service.client.catalog(service.client.configuration(), actor, keyword, page, size)


@router.get("/application-market/applications/{application_id}")
def detail(application_id: str, request: Request):
    return market(request).detail(application_id, current_actor(request))


@router.get("/admin/application-market/applications/{application_id}")
def admin_detail(application_id: str, request: Request):
    return market(request).detail(application_id, current_actor(request, True), admin=True)


@router.get("/application-market/applications/{application_id}/versions")
def versions(
    application_id: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    return market(request).versions(application_id, current_actor(request), page, size)


@router.get("/admin/application-market/categories")
def categories(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    service, actor = market(request), current_actor(request, True)
    # 分类管理面可能跨租户，只向系统管理员提供此查询。
    if actor.role != "admin":
        raise HTTPException(403, "只有系统管理员可以读取外部分类目录")
    if service.settings.applications_market != "himarket":
        raise HTTPException(503, "外部目录服务尚未启用")
    return service.client.categories(service.client.configuration(), page, size)


@router.post("/application-market/applications/{application_id}/operations")
def operate(application_id: str, payload: MarketMutation, request: Request):
    return market(request).execute(application_id, current_actor(request), payload)


@router.post("/admin/application-market/applications/{application_id}/operations")
def admin_operate(application_id: str, payload: MarketMutation, request: Request):
    return market(request).execute(
        application_id, current_actor(request, True), payload, admin=True
    )


@router.post(
    "/application-market/applications/{application_id}/operations/{operation_id}/reconcile"
)
def reconcile(
    application_id: str, operation_id: str, request: Request, payload: MarketRecovery | None = None
):
    return market(request).reconcile(
        application_id, operation_id, current_actor(request), recovery=payload
    )


@router.post(
    "/admin/application-market/applications/{application_id}/operations/{operation_id}/reconcile"
)
def admin_reconcile(
    application_id: str, operation_id: str, request: Request, payload: MarketRecovery | None = None
):
    return market(request).reconcile(
        application_id, operation_id, current_actor(request, True), admin=True, recovery=payload
    )
