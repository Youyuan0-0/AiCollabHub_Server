"""首页和运营基础接口；发布权限与读取权限复用已有受信身份体系。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.core.auth import current_principal, require_permissions
from app.db.session import SessionDependency
from app.schemas.overview import (
    BudgetInput,
    ComputeAnalysis,
    HomeOverview,
    HomeOverviewResponse,
    OperationsOverviewResponse,
    RankingPeriod,
    Rankings,
    RankingType,
    SnapshotReceipt,
    TokenAnalysis,
    TokenDimension,
    TokenPeriod,
)
from app.services.overview import get_home, get_operations, save_snapshot

# 仅两个 GET 支持非生产匿名读取，发布端点始终使用原有可信身份及权限校验。
router = APIRouter(
    prefix="/overview",
    tags=["overview"],
    responses={
        401: {"description": "尚未登录或未配置可信身份提供器"},
        403: {"description": "缺少看板读取或管理权限"},
        503: {"description": "数据库未配置、未迁移、不可用或快照损坏"},
    },
)
write_dependencies = [Depends(require_permissions("overview:read", "overview:write"))]


async def require_overview_read(request: Request) -> None:
    """非生产环境只放行看板读取；生产或环境标记缺失时仍要求真实身份和读取权限。"""
    if getattr(request.app.state, "app_env", "production") in {"development", "test"}:
        return
    principal = await current_principal(request)
    require_permissions("overview:read")(principal)


# 读写共用筛选参数定义，确保别名与可选值一致；默认值在各端点声明。
TokenPeriodQuery = Annotated[TokenPeriod, Query(alias="tokenPeriod", description="Token 统计周期")]
TokenDimensionQuery = Annotated[
    TokenDimension, Query(alias="tokenDimension", description="Token 分析维度")
]
RankingTypeQuery = Annotated[RankingType, Query(alias="rankingType", description="运营排行类型")]
RankingPeriodQuery = Annotated[RankingPeriod, Query(alias="rankingPeriod", description="排行周期")]


@router.get(
    "/home",
    response_model=HomeOverviewResponse,
    summary="获取首页概览",
    dependencies=[Depends(require_overview_read)],
)
def home_overview(request: Request, session: SessionDependency) -> HomeOverviewResponse:
    """返回资源数量、Token、算力、推荐、动态、场景、问题建议和七类首页榜单。"""
    # 仅服务端非生产环境可补充固定样例，生产请求不能通过查询参数启用。
    return get_home(
        session,
        use_demo=getattr(request.app.state, "app_env", "production") in {"development", "test"},
    )


@router.get(
    "/operations",
    response_model=OperationsOverviewResponse,
    summary="获取运营详情",
    dependencies=[Depends(require_overview_read)],
)
def operations_overview(
    request: Request,
    session: SessionDependency,
    token_period: TokenPeriodQuery = "月",
    token_dimension: TokenDimensionQuery = "组织",
    ranking_type: RankingTypeQuery = "用户活跃榜",
    ranking_period: RankingPeriodQuery = "周榜",
) -> OperationsOverviewResponse:
    """筛选值与前端一致；非法值返回 422，非生产缺失板块返回原前端固定样例。"""
    return get_operations(
        session,
        token_period,
        token_dimension,
        ranking_type,
        ranking_period,
        use_demo=getattr(request.app.state, "app_env", "production") in {"development", "test"},
    )


@router.put(
    "/home",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布首页完整快照",
)
def update_home(data: HomeOverview, session: SessionDependency) -> SnapshotReceipt:
    """data 为完整首页数据，数组顺序即展示顺序；本阶段计数由可信统计方录入。"""
    return save_snapshot(session, "home", "default", data)


@router.put(
    "/operations/token",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布指定周期和维度的 Token 分析",
)
def update_token(
    data: TokenAnalysis,
    session: SessionDependency,
    token_period: TokenPeriodQuery = "月",
    token_dimension: TokenDimensionQuery = "组织",
) -> SnapshotReceipt:
    """只替换选中的周期与维度，其他组合保持原值；不按固定倍率伪造日周统计。"""
    return save_snapshot(session, "token", f"{token_period}:{token_dimension}", data)


@router.put(
    "/operations/compute",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布算力监控快照",
)
def update_compute(data: ComputeAnalysis, session: SessionDependency) -> SnapshotReceipt:
    """更新节点负载、汇总及资源池；本阶段由监控适配器或运营方主动推送。"""
    return save_snapshot(session, "compute", "default", data)


@router.put(
    "/operations/budget",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="更新月度预算",
)
def update_budget(data: BudgetInput, session: SessionDependency) -> SnapshotReceipt:
    """只接受已用金额、限额和预警阈值，查询时统一计算预算使用率及提示。"""
    return save_snapshot(session, "budget", "default", data)


@router.put(
    "/operations/rankings",
    response_model=SnapshotReceipt,
    dependencies=write_dependencies,
    summary="发布指定类型和周期的运营排行",
)
def update_rankings(
    data: Rankings,
    session: SessionDependency,
    ranking_type: RankingTypeQuery = "用户活跃榜",
    ranking_period: RankingPeriodQuery = "周榜",
) -> SnapshotReceipt:
    """每行是名称与指标，按请求顺序发布；空数组可清空选中榜单。"""
    return save_snapshot(session, "rankings", f"{ranking_type}:{ranking_period}", data)
