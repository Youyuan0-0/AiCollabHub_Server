"""仅开发用的内存仓储，无数据库、文件写入或网络访问。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock


# 一次内存事务的读写入口；state 是仓储快照，正常结束后才成为正式状态。
class MemorySession:
    """内存仓储快照的读写入口，所有对外资料均使用独立副本。"""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.resource_warnings: list[str] = []

    def entries(self, kind: str) -> list[dict]:
        """读取指定种类的活动记录副本。"""
        return deepcopy(list(self.state["activities"].get(kind, {}).values()))

    def entry(self, kind: str, key: str) -> dict | None:
        """按业务键读取活动记录副本，不存在时返回空值。"""
        return deepcopy(self.state["activities"].get(kind, {}).get(key))

    def interaction_entries(
        self, subject: str | None = None, resource_id: str | None = None
    ) -> list[dict]:
        """按同一仓储契约筛选互动副本，不泄露其他用户或资源记录。"""
        return [
            item
            for item in self.entries("interactions")
            if (subject is None or item.get("actorId", item.get("userId")) == subject)
            and (
                resource_id is None
                or any(
                    item.get(key) == resource_id
                    for key in ("resourceId", "applicationId", "datasetId", "toolId")
                )
            )
        ]

    def put_entry(self, kind: str, key: str, value: dict) -> None:
        """将活动记录副本保存到当前事务快照。"""
        self.state["activities"].setdefault(kind, {})[key] = deepcopy(value)

    def review_files(self, publication_id: str) -> list[dict]:
        """读取指定申请的报告索引副本。"""
        return [
            item for item in self.entries("review_files") if item["publicationId"] == publication_id
        ]

    def remove_review_file(self, key: str) -> None:
        """移除未提交的报告索引。"""
        self.state["activities"].get("review_files", {}).pop(key, None)

    def list_resources(self, resource_ids: list[str] | None = None) -> list[dict]:
        """读取指定范围的资源目录副本。"""
        selected = set(resource_ids) if resource_ids is not None else None
        return deepcopy(
            [
                item
                for key, item in self.state["resources"].items()
                if selected is None or key in selected
            ]
        )

    def resource_states(self, resource_ids: list[str] | None = None) -> dict[str, dict]:
        """只返回启停状态和修订，与数据库仓储保持相同形状。"""
        return {
            item["id"]: {
                "listingStatus": item.get("listingStatus", "listed"),
                "revision": item.get("revision", 1),
            }
            for item in self.list_resources(resource_ids)
        }

    def get_resource(self, resource_id: str) -> dict | None:
        """按资源编号读取资源副本。"""
        return deepcopy(self.state["resources"].get(resource_id))

    def put_resource(self, resource: dict) -> None:
        """保存资源副本，避免后续外部修改污染快照。"""
        self.state["resources"][resource["id"]] = deepcopy(resource)

    def list_publications(self, *, warnings: list[str] | None = None) -> list[dict]:
        """读取全部发布申请的独立副本。"""
        return deepcopy(list(self.state["publications"].values()))

    def get_publication(self, publication_id: str) -> dict | None:
        """按申请编号读取独立副本。"""
        return deepcopy(self.state["publications"].get(publication_id))

    # 保存申请副本；后续 GET 通过 get_publication/list_publications 读取。
    def put_publication(self, publication: dict) -> None:
        """保存发布申请副本，供后续事务查询。"""
        self.state["publications"][publication["id"]] = deepcopy(publication)

    def get_visibility(self, resource_id: str) -> dict:
        """读取开放范围，未配置时使用默认的全部可见规则。"""
        return deepcopy(
            self.state["visibility"].get(
                resource_id,
                {"scope": "all", "targets": [], "revision": 1},
            )
        )

    def put_visibility(self, resource_id: str, visibility: dict) -> None:
        """保存开放范围配置的独立副本。"""
        self.state["visibility"][resource_id] = deepcopy(visibility)

    def get_configuration(self) -> dict:
        """读取发布配置的独立副本。"""
        return deepcopy(self.state["configuration"])

    def put_configuration(self, configuration: dict) -> None:
        """保存发布配置的独立副本。"""
        self.state["configuration"] = deepcopy(configuration)


# 数据保存在 Python 进程内；服务重启会重置，不同后端实例之间不共享。
class InMemoryResourceRepository:
    """仅供开发使用的进程内仓储，重启后重置且不跨实例共享。"""

    def __init__(
        self, *, resources: list[dict] | None = None, configuration: dict | None = None
    ) -> None:
        """允许工具模块注入独立样例，复用事务实现且不混用资源数据。"""
        self._lock = RLock()
        self._state = {
            "resources": {item["id"]: deepcopy(item) for item in (resources or [])},
            "publications": {},
            "activities": {},
            "visibility": {},
            "configuration": deepcopy(configuration or {}),
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
