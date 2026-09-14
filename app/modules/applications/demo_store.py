"""开发组合入口：业务层仅依赖 ports，替换存储与供应方无需改页面路由。"""

from app.modules.applications.application_service import ApplicationService
from app.modules.applications.memory_repository import InMemoryApplicationRepository
from app.modules.applications.providers import DemoApplicationProvider


# main.create_app() 启动时调用：组装业务类、内存仓储和演示资源适配器。
def create_demo_application_service() -> ApplicationService:
    return ApplicationService(InMemoryApplicationRepository(), DemoApplicationProvider())
