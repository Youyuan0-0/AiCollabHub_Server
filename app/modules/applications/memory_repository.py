"""应用模块只负责注入自己的样例和默认配置，事务实现复用公共层。"""

from app.modules.applications.configuration import default_configuration
from app.modules.applications.sample_data import seed_applications
from app.modules.common.memory_repository import InMemoryResourceRepository


class InMemoryApplicationRepository(InMemoryResourceRepository):
    """保留应用仓储装配入口，工具不再依赖此默认配置。"""

    def __init__(
        self, *, resources: list[dict] | None = None, configuration: dict | None = None
    ) -> None:
        super().__init__(
            resources=seed_applications() if resources is None else resources,
            configuration=default_configuration() if configuration is None else configuration,
        )
