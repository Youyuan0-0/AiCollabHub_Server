"""替换约定：字典遵循平台 camelCase DTO；事务提交前不泄露可变引用。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class ResourceSession(Protocol):
    """一次资源事务内的读写契约，返回资料不得泄露仓储内部可变引用。"""

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
        """读取已发布的资源目录。"""
        ...

    def get_resource(self, resource_id: str) -> dict | None:
        """按资源编号读取目录资源，不存在时返回空值。"""
        ...

    def put_resource(self, resource: dict) -> None:
        """在当前事务中保存已发布资源资料。"""
        ...

    def list_publications(self, *, warnings: list[str] | None = None) -> list[dict]:
        """读取发布申请列表。"""
        ...

    def get_publication(self, publication_id: str) -> dict | None:
        """按申请编号读取草稿或审核资料，不存在时返回空值。"""
        ...

    def put_publication(self, publication: dict) -> None:
        """在当前事务中保存发布申请和审核状态。"""
        ...

    def get_visibility(self, resource_id: str) -> dict:
        """读取资源开放范围及配置版本。"""
        ...

    def put_visibility(self, resource_id: str, visibility: dict) -> None:
        """保存资源开放范围及配置版本。"""
        ...

    def get_configuration(self) -> dict:
        """读取发布表单与管理列配置。"""
        ...

    def put_configuration(self, configuration: dict) -> None:
        """保存发布表单与管理列配置。"""
        ...


class ResourceRepository(Protocol):
    """资源仓储契约，统一提供原子读写事务和只读快照。"""

    def transaction(self, *, is_readonly: bool = False) -> AbstractContextManager[ResourceSession]:
        """原子提交/回滚；须串行化同一资源/申请/配置的并发写入。

        数据库实现需事务、行锁/乐观锁和唯一约束，不能用普通 SELECT 后 UPDATE
        冒充本约定。审核结果、审核快照和已发布版本必须一起提交。
        readonly 使用一致的只读快照，不获取写锁，也不提交业务修改。
        """
        ...
