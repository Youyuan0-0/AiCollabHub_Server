"""真实外部发布入口；复用平台认证、CSRF 与应用归属检查。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.applications.application_service import application_service
from app.modules.applications.identity import current_actor
from app.modules.applications.market_contract import MarketMutation, MarketRecovery
from app.modules.applications.market_workflows import MarketWorkflows

router = APIRouter(tags=["application external publishing"])


def market(request: Request) -> MarketWorkflows:
    """获取请求使用的外部发布服务，支持测试注入传输实现。"""
    # 测试可注入 HTTP transport；正式入口只使用受控服务端配置。
    if hasattr(request.app.state, "application_market"):
        return request.app.state.application_market
    return MarketWorkflows(application_service(request), request.app.state.settings)


@router.get("/application-market", response_model=None)
def status(request: Request) -> dict:
    """查询当前用户可用的外部发布服务与应用。"""
    return market(request).status(current_actor(request))


@router.get("/admin/application-market", response_model=None)
def admin_status(request: Request) -> dict:
    """校验管理员身份后查询外部发布服务状态。"""
    return market(request).status(current_actor(request, True), is_admin=True)


@router.get("/application-market/catalog", response_model=None)
def catalog(
    request: Request,
    keyword: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=12, ge=1, le=100),
) -> dict:
    """使用当前用户的外部开发者凭据分页搜索目录。"""
    actor, service = current_actor(request), market(request)
    if service.settings.applications_market != "himarket":
        raise HTTPException(503, "外部目录服务尚未启用")
    return service.client.catalog(service.client.configuration(), actor, keyword, page, size)


@router.get("/application-market/applications/{application_id}", response_model=None)
def detail(application_id: str, request: Request) -> dict:
    """读取当前用户有权操作的应用外部发布详情。"""
    return market(request).detail(application_id, current_actor(request))


@router.get("/admin/application-market/applications/{application_id}", response_model=None)
def admin_detail(application_id: str, request: Request) -> dict:
    """按管理权限读取应用外部条目及操作记录。"""
    return market(request).detail(application_id, current_actor(request, True), is_admin=True)


@router.get("/application-market/applications/{application_id}/versions", response_model=None)
def versions(
    application_id: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """分页查询当前应用绑定资源的外部版本。"""
    return market(request).versions(application_id, current_actor(request), page, size)


@router.get("/admin/application-market/categories", response_model=None)
def categories(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """仅允许系统管理员分页读取外部分类目录。"""
    service, actor = market(request), current_actor(request, True)
    # 分类管理面可能跨租户，只向系统管理员提供此查询。
    if actor.role != "admin":
        raise HTTPException(403, "只有系统管理员可以读取外部分类目录")
    if service.settings.applications_market != "himarket":
        raise HTTPException(503, "外部目录服务尚未启用")
    return service.client.categories(service.client.configuration(), page, size)


@router.post("/application-market/applications/{application_id}/operations", response_model=None)
def operate(application_id: str, payload: MarketMutation, request: Request) -> dict:
    """按当前用户权限提交外部草稿、审核或发布操作。"""
    return market(request).execute(application_id, current_actor(request), payload)


@router.post(
    "/admin/application-market/applications/{application_id}/operations", response_model=None
)
def admin_operate(application_id: str, payload: MarketMutation, request: Request) -> dict:
    """按管理权限执行外部审核与发布操作。"""
    return market(request).execute(
        application_id, current_actor(request, True), payload, is_admin=True
    )


@router.post(
    "/application-market/applications/{application_id}/operations/{operation_id}/reconcile",
    response_model=None,
)
def reconcile(
    application_id: str, operation_id: str, request: Request, payload: MarketRecovery | None = None
) -> dict:
    """校验用户权限并重新核对外部操作结果。"""
    return market(request).reconcile(
        application_id, operation_id, current_actor(request), recovery=payload
    )


@router.post(
    "/admin/application-market/applications/{application_id}/operations/{operation_id}/reconcile",
    response_model=None,
)
def admin_reconcile(
    application_id: str, operation_id: str, request: Request, payload: MarketRecovery | None = None
) -> dict:
    """按管理权限核对外部操作及补充的审核回执。"""
    return market(request).reconcile(
        application_id, operation_id, current_actor(request, True), is_admin=True, recovery=payload
    )
