"""五类资源共用的事务仓储，主表仍按模块独立，互动和审核使用共用表。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy

from sqlalchemy import Table
from sqlalchemy.orm import Session

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase
from app.modules.common.resource_activity import ResourceActivity
from app.modules.common.resource_catalog import ResourceCatalog
from app.modules.common.resource_records import ResourceRecords
from app.modules.common.resource_runtime import ResourceRuntime


class ResourceDatabaseSession(ResourceRecords, ResourceCatalog, ResourceActivity):
    """将现有业务 DTO 映射到新表，整个发布或审核流程共享一个数据库事务。"""

    def __init__(
        self, repository: ResourceDatabase, session: Session, *, is_readonly: bool
    ) -> None:
        self.repository = repository
        self.foundation = repository.foundation
        self.db = session
        self.kind = repository.kind
        self.main_name, self.prefix, self.module = RESOURCE_TABLES[self.kind]
        self.resource_key = {
            "dataset": "datasetId",
            "application": "applicationId",
            "tool": "toolId",
        }.get(self.kind, "resourceId")
        self.is_readonly = is_readonly
        self.resource_warnings: list[str] = []
        self.pending_resources: dict[str, dict] = {}

    def table(self, name: str) -> Table:
        """取得本事务使用的新基线表。"""
        return self.foundation.table(name)

    def lock(self, key: str) -> None:
        """写事务锁定业务对象，只读事务不争用写锁。"""
        if not self.is_readonly:
            self.foundation.lock(self.db, self.kind + ":" + key)

    def flush_resources(self) -> None:
        """提交前保存纯管理状态变更，正式资料必须经由审核和版本发布。"""
        for resource in list(self.pending_resources.values()):
            self.save_resource_state(resource)
        self.pending_resources.clear()


class ResourceDatabase:
    """按模块装配仓储，复用连接池和已经确认的新表结构。"""

    def __init__(
        self,
        foundation: FoundationDatabase,
        kind: str,
        *,
        configuration: dict | None = None,
        runtime: ResourceRuntime | None = None,
    ) -> None:
        if kind not in RESOURCE_TABLES:
            raise ValueError("Unknown resource module")
        self.foundation = foundation
        self.kind = kind
        self.configuration = deepcopy(configuration or {})
        self.runtime = runtime

    @contextmanager
    def transaction(self, *, is_readonly: bool = False) -> Iterator[ResourceDatabaseSession]:
        """异常时回滚全部关联修改，外部工具调用仍由服务放在事务之外。"""
        with self.foundation.transaction(is_readonly=is_readonly) as session:
            unit = ResourceDatabaseSession(self, session, is_readonly=is_readonly)
            yield unit
            if not is_readonly:
                unit.flush_resources()
