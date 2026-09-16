"""开发组合入口：业务层仅依赖 ports，替换存储与供应方无需改页面路由。"""

from __future__ import annotations

from sqlalchemy import Engine

from app.core.config import Settings
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.memory_repository import InMemoryApplicationRepository
from app.modules.applications.providers import DemoApplicationProvider


# main.create_app() 启动时调用：组装业务类、内存仓储和演示资源适配器。
def create_demo_application_service() -> ApplicationService:
    """组合内存仓储与演示供应方，创建应用业务服务。"""
    return ApplicationService(InMemoryApplicationRepository(), DemoApplicationProvider())


def create_database_application_service(engine: Engine, settings: Settings) -> ApplicationService:
    """平台数据存 PostgreSQL；实际接口缺席期间，外部资料仍由开发模拟器提供。"""
    from app.modules.applications.database_repository import DatabaseApplicationRepository
    from app.modules.applications.demo_marketplace import DemoMarketplaceStore

    marketplace = DemoMarketplaceStore(settings.applications_demo_directory)
    return ApplicationService(
        DatabaseApplicationRepository(engine, settings, marketplace),
        DemoApplicationProvider(marketplace),
    )
