from fastapi import APIRouter

from app.api.health import router as health_router
from app.modules.admin.router import router as admin_router
from app.modules.applications.router import router as applications_router
from app.modules.applications.workflow_router import router as application_workflow_router
from app.modules.common.router import router as common_router
from app.modules.datasets.router import router as datasets_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.models.router import router as models_router
from app.modules.tools.router import router as tools_router

# 平台接口统一使用版本前缀，后续业务模块的路由在此集中挂载。
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)

# Mock 路由与基础设施探测分离；七个模块共用 /api/v1 契约，内部按领域组织。
# 应用模块的 /application-publications 在这里挂载后，完整地址带上 /api/v1 前缀。
mock_router = APIRouter(prefix="/api/v1")
for router in (
    datasets_router,
    models_router,
    applications_router,
    application_workflow_router,
    knowledge_router,
    tools_router,
    common_router,
    admin_router,
):
    mock_router.include_router(router)
