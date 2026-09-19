"""替换约定：字典遵循平台 camelCase DTO；事务提交前不泄露可变引用。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class ApplicationSession(Protocol):
    """一次应用事务内的读写契约，返回资料不得泄露仓储内部可变引用。"""

    resource_warnings: list[str]

    def entries(self, kind: str) -> list[dict]:
        """读取指定业务种类的全部活动记录。"""
        ...

    def entry(self, kind: str, key: str) -> dict | None:
        """按业务种类和幂等键读取活动记录，不存在时返回空值。"""
        ...

    def put_entry(self, kind: str, key: str, value: dict) -> None:
        """在当前事务中写入活动记录。"""
        ...

    def review_files(self, publication_id: str) -> list[dict]:
        """读取指定申请的报告附件索引。"""
        ...

    def remove_review_file(self, key: str) -> None:
        """在事务中移除未提交的报告索引。"""
        ...

    def list_resources(self) -> list[dict]:
        """读取已发布的应用目录。"""
        ...

    def get_resource(self, application_id: str) -> dict | None:
        """按应用编号读取目录资源，不存在时返回空值。"""
        ...

    def put_resource(self, resource: dict) -> None:
        """在当前事务中保存已发布应用资料。"""
        ...

    def list_publications(self) -> list[dict]:
        """读取发布申请列表。"""
        ...

    def get_publication(self, publication_id: str) -> dict | None:
        """按申请编号读取草稿或审核资料，不存在时返回空值。"""
        ...

    def put_publication(self, publication: dict) -> None:
        """在当前事务中保存发布申请和审核状态。"""
        ...

    def get_visibility(self, application_id: str) -> dict:
        """读取应用开放范围及配置版本。"""
        ...

    def put_visibility(self, application_id: str, visibility: dict) -> None:
        """保存应用开放范围及配置版本。"""
        ...

    def get_configuration(self) -> dict:
        """读取发布表单与管理列配置。"""
        ...

    def put_configuration(self, configuration: dict) -> None:
        """保存发布表单与管理列配置。"""
        ...


class ApplicationRepository(Protocol):
    """应用仓储契约，统一提供原子读写事务和只读快照。"""

    def transaction(
        self, *, is_readonly: bool = False
    ) -> AbstractContextManager[ApplicationSession]:
        """原子提交/回滚；须串行化同一应用/申请/配置的并发写入。

        数据库实现需事务、行锁/乐观锁和唯一约束，不能用普通 SELECT 后 UPDATE
        冒充本约定。审核结果、审核快照和已发布版本必须一起提交。
        readonly 使用一致的只读快照，不获取写锁，也不提交业务修改。
        """
        ...


class ApplicationProvider(Protocol):
    """关联资源、工具链和应用运行的供应方适配契约。"""

    def dependencies(self) -> list[dict]:
        """读取可关联资源的目录资料。"""
        ...

    def validate_dependencies(self, dependencies: list[dict]) -> None:
        """校验关联资源的类型与编号，失败时抛出业务异常。"""
        ...

    def runtime_status(self, application_id: str, version_id: str) -> dict:
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
