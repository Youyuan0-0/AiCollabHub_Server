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
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"


class DependencyHealthResponse(BaseModel):
    status: Literal["ready", "degraded"]
    dependencies: dict[str, Literal["available", "unavailable", "not_configured", "disabled"]]


# 存活检查不依赖数据库，避免数据库故障被误判为应用进程故障。
@router.get("/live", response_model=LivenessResponse)
def liveness() -> LivenessResponse:
    return LivenessResponse()


# 就绪检查仅验证数据库连接，不代表业务表已迁移或外部 AI 服务可用。
@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"description": "Database is unconfigured or unavailable."}},
)
def readiness(engine: Annotated[Engine, Depends(get_engine)]) -> ReadinessResponse:
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
    if request.app.state.settings.app_env == "production":
        raise HTTPException(404, "not_found")
    resources = request.app.state.resources
    states = {
        "database": "not_configured",
        "replica": "disabled",
        "read_fallback": "disabled",
        "redis": "not_configured",
        "minio": "not_configured",
    }
    if resources is not None:
        if resources.database is not None:
            pools = resources.database
            states["database"] = "available" if pools.probe(pools.primary) else "unavailable"
            for name, engine in (("replica", pools.replica), ("read_fallback", pools.fallback)):
                if engine is not None:
                    states[name] = "available" if pools.probe(engine) else "unavailable"
        for name, resource in (("redis", resources.cache), ("minio", resources.storage)):
            if resource is not None:
                states[name] = "available" if resource.probe() else "unavailable"
    healthy = states["database"] == "available" and "unavailable" not in states.values()
    # 只报告连接或桶的可访问性，不冒充迁移版本、复制同步和业务权限验收。
    return DependencyHealthResponse(status="ready" if healthy else "degraded", dependencies=states)
