from fastapi import APIRouter

from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.overview import router as overview_router
from app.modules.admin.router import router as admin_router
from app.modules.applications.market_router import router as application_market_router
from app.modules.applications.review_file_router import router as application_review_file_router
from app.modules.applications.router import router as applications_router
from app.modules.applications.tutorial_router import router as application_tutorial_router
from app.modules.applications.workflow_router import router as application_workflow_router
from app.modules.common.toolchain.configuration_router import (
    router as toolchain_configuration_router,
)
from app.modules.datasets.admin_router import router as dataset_admin_router
from app.modules.datasets.report_router import router as dataset_report_router
from app.modules.datasets.router import router as datasets_router
from app.modules.datasets.toolchain_router import router as dataset_toolchain_router
from app.modules.feedback.router import router as feedback_router
from app.modules.knowledge.admin_router import router as knowledge_admin_router
from app.modules.knowledge.router import router as knowledge_router
from app.modules.models.admin_router import router as models_admin_router
from app.modules.models.router import router as models_router
from app.modules.profile.router import router as profile_router
from app.modules.resources.router import router as resources_router
from app.modules.tools.admin_router import router as tools_admin_router
from app.modules.tools.report_router import router as tools_report_router
from app.modules.tools.router import router as tools_router

# 平台接口统一使用版本前缀，后续业务模块的路由在此集中挂载。
api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
# 数据库业务单独挂载，create_app 为其接入与场景应用相同的登录和 CSRF 检查。
# 健康检查不依赖登录；看板、会话仍保留各自的环境限制。
database_router = APIRouter(prefix="/api/v1")
database_router.include_router(overview_router)
database_router.include_router(conversations_router)
database_router.include_router(feedback_router)

# Mock 路由与基础设施探测分离；七个模块共用 /api/v1 契约，内部按领域组织。
# 应用模块的 /application-publications 在这里挂载后，完整地址带上 /api/v1 前缀。
mock_router = APIRouter(prefix="/api/v1")
for router in (
    toolchain_configuration_router,
    datasets_router,
    dataset_toolchain_router,
    dataset_admin_router,
    dataset_report_router,
    models_router,
    models_admin_router,
    applications_router,
    application_tutorial_router,
    application_review_file_router,
    application_workflow_router,
    application_market_router,
    knowledge_router,
    knowledge_admin_router,
    tools_router,
    tools_admin_router,
    tools_report_router,
    resources_router,
    profile_router,
    admin_router,
):
    mock_router.include_router(router)
