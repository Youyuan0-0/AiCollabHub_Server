import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
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
