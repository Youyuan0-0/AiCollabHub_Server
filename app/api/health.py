import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import Engine, literal, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    """进程存活检查结果。"""

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    """数据库连接就绪检查结果。"""

    status: Literal["ready"] = "ready"


class DependencyHealthResponse(BaseModel):
    """开发环境的外部依赖诊断结果。"""

    status: Literal["ready", "degraded"]
    dependencies: dict[str, Literal["available", "unavailable", "not_configured", "disabled"]]


@router.get("/live", response_model=LivenessResponse)
def liveness() -> LivenessResponse:
    """仅检查应用进程存活，避免把数据库故障误判为进程故障。"""
    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"description": "Database is unconfigured or unavailable."}},
)
def readiness(engine: Annotated[Engine, Depends(get_engine)]) -> ReadinessResponse:
    """验证数据库连接；不代表业务表已迁移或外部 AI 服务可用。"""
    try:
        with engine.connect() as connection:
            # 由当前数据库方言生成探测 SQL，避免硬编码特定数据库语法。
            connection.execute(select(literal(1)))
    except SQLAlchemyError as exc:
        # 仅记录异常类型，避免连接信息或 SQL 参数进入日志与接口响应。
        logger.warning("Database readiness probe failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
        ) from None
    return ReadinessResponse()


@router.get(
    "/dependencies",
    response_model=DependencyHealthResponse,
)
def dependency_health(request: Request) -> DependencyHealthResponse:
    """诊断已配置依赖的可访问性；生产环境不开放此接口。"""
    if request.app.state.settings.app_env == "production":
        raise HTTPException(404, "not_found")
    resources = request.app.state.resources
    states = {
        "database": "not_configured",
        "redis": "not_configured",
        "minio": "not_configured",
    }
    if resources is not None:
        if resources.database is not None:
            try:
                with resources.database.connect() as connection:
                    connection.execute(select(literal(1)))
                states["database"] = "available"
            except SQLAlchemyError:
                states["database"] = "unavailable"
        for name, resource in (("redis", resources.cache), ("minio", resources.storage)):
            if resource is not None:
                states[name] = "available" if resource.probe() else "unavailable"
    healthy = states["database"] == "available" and "unavailable" not in states.values()
    # 只报告连接或桶的可访问性，不代表迁移版本和业务权限验收。
    return DependencyHealthResponse(status="ready" if healthy else "degraded", dependencies=states)
