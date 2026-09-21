"""应用供应方契约；事务接口由公共资源层提供。"""

from typing import Protocol

from app.modules.common.transaction_ports import ResourceRepository as ApplicationRepository
from app.modules.common.transaction_ports import ResourceSession as ApplicationSession

__all__ = ["ApplicationRepository", "ApplicationSession", "ApplicationProvider"]


class ApplicationProvider(Protocol):
    """关联资源、工具链和应用运行的供应方适配契约。"""

    def dependencies(self) -> list[dict]:
        """读取可关联资源的目录资料。"""
        ...

    def validate_dependencies(self, dependencies: list[dict]) -> None:
        """校验关联资源的类型与编号，失败时抛出业务异常。"""
        ...

    def runtime_status(self, resource: dict) -> dict:
        """返回接入状态和可空的运行指标；无统计时返回 None，不能生成虚构值。"""
        ...

    def toolchain_resources(self) -> list[dict]:
        """读取可导入的工具链资源与版本。"""
        ...

    def toolchain_resource(self, external_id: str, version: str) -> dict:
        """按外部编号和版本读取工具链资料。"""
        ...

    def invoke(self, resource: dict, inputs: dict, options: dict, simulate_failure: bool) -> dict:
        """使用指定输入和配置执行应用，返回供应方适配后的结果。"""
        ...

    def develop(self, data: dict, actor_id: str) -> dict:
        """提交开发资料到工具链，返回外部资源与版本编号。"""
        ...
