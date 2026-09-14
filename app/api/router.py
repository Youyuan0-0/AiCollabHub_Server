from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.health import router as health_router

# 平台接口统一使用版本前缀，后续业务模块的路由在此集中挂载。
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
