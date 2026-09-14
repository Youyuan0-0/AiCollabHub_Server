from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.overview import router as overview_router

# 平台接口统一使用版本前缀，后续业务模块的路由在此集中挂载。
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
# 首页与运营看板沿用前端已经预留的 /overview 契约。
api_router.include_router(overview_router)

# 开发问答与历史独立于看板，演示环境限制在对应路由中声明。
api_router.include_router(conversations_router)
