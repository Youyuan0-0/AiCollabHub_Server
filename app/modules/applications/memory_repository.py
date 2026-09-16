"""仅开发用的内存仓储，无数据库、文件写入或网络访问。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from app.modules.applications.configuration import default_configuration
from app.modules.applications.sample_data import seed_applications


# 一次内存事务的读写入口；state 是仓储快照，正常结束后才成为正式状态。
class MemorySession:
    """内存仓储快照的读写入口，所有对外资料均使用独立副本。"""

    def __init__(self, state: dict) -> None:
        self.state = state

    def entries(self, kind: str) -> list[dict]:
        """读取指定种类的活动记录副本。"""
        return deepcopy(list(self.state["activities"].get(kind, {}).values()))

    def entry(self, kind: str, key: str) -> dict | None:
        """按业务键读取活动记录副本，不存在时返回空值。"""
        return deepcopy(self.state["activities"].get(kind, {}).get(key))

    def put_entry(self, kind: str, key: str, value: dict) -> None:
        """将活动记录副本保存到当前事务快照。"""
        self.state["activities"].setdefault(kind, {})[key] = deepcopy(value)

    def list_resources(self) -> list[dict]:
        """读取应用目录的独立副本。"""
        return deepcopy(list(self.state["resources"].values()))

    def get_resource(self, application_id: str) -> dict | None:
        """按应用编号读取资源副本。"""
        return deepcopy(self.state["resources"].get(application_id))

    def put_resource(self, resource: dict) -> None:
        """保存资源副本，避免后续外部修改污染快照。"""
        self.state["resources"][resource["id"]] = deepcopy(resource)

    def list_publications(self) -> list[dict]:
        """读取全部发布申请的独立副本。"""
        return deepcopy(list(self.state["publications"].values()))

    def get_publication(self, publication_id: str) -> dict | None:
        """按申请编号读取独立副本。"""
        return deepcopy(self.state["publications"].get(publication_id))

    # 保存申请副本；后续 GET 通过 get_publication/list_publications 读取。
    def put_publication(self, publication: dict) -> None:
        """保存发布申请副本，供后续事务查询。"""
        self.state["publications"][publication["id"]] = deepcopy(publication)

    def get_visibility(self, application_id: str) -> dict:
        """读取开放范围，未配置时使用默认的全部可见规则。"""
        return deepcopy(
            self.state["visibility"].get(
                application_id,
                {"scope": "all", "targets": [], "revision": 1},
            )
        )

    def put_visibility(self, application_id: str, visibility: dict) -> None:
        """保存开放范围配置的独立副本。"""
        self.state["visibility"][application_id] = deepcopy(visibility)

    def get_configuration(self) -> dict:
        """读取发布配置的独立副本。"""
        return deepcopy(self.state["configuration"])

    def put_configuration(self, configuration: dict) -> None:
        """保存发布配置的独立副本。"""
        self.state["configuration"] = deepcopy(configuration)


# 数据保存在 Python 进程内；服务重启会重置，不同后端实例之间不共享。
class InMemoryApplicationRepository:
    """仅供开发使用的进程内仓储，重启后重置且不跨实例共享。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._state = {
            "resources": {item["id"]: item for item in seed_applications()},
            "publications": {},
            "activities": {},
            "visibility": {},
            "configuration": default_configuration(),
        }

    @contextmanager
    def transaction(self, *, is_readonly: bool = False) -> Iterator[MemorySession]:
        """串行处理快照，正常结束时提交写事务，异常或只读事务不回写。"""
        with self._lock:
            # 先在副本上处理；发生异常时不把部分修改写回正式状态。
            snapshot = deepcopy(self._state)
            yield MemorySession(snapshot)
            # 异常时不执行本行；防止审核和资源更新只完成一半。
            if not is_readonly:
                self._state = snapshot
