"""仅开发用的内存仓储，无数据库、文件写入或网络访问。"""

from contextlib import contextmanager
from copy import deepcopy
from threading import RLock

from app.modules.applications.configuration import default_configuration
from app.modules.applications.sample_data import seed_applications


# 一次内存事务的读写入口；state 是仓储快照，正常结束后才成为正式状态。
class MemorySession:
    def __init__(self, state: dict):
        self.state = state

    def entries(self, kind):
        return deepcopy(list(self.state["activities"].get(kind, {}).values()))

    def entry(self, kind, key):
        return deepcopy(self.state["activities"].get(kind, {}).get(key))

    def put_entry(self, kind, key, value):
        self.state["activities"].setdefault(kind, {})[key] = deepcopy(value)

    def list_resources(self):
        return deepcopy(list(self.state["resources"].values()))

    def get_resource(self, application_id):
        return deepcopy(self.state["resources"].get(application_id))

    def put_resource(self, resource):
        self.state["resources"][resource["id"]] = deepcopy(resource)

    def list_publications(self):
        return deepcopy(list(self.state["publications"].values()))

    def get_publication(self, publication_id):
        return deepcopy(self.state["publications"].get(publication_id))

    # 保存申请副本；后续 GET 通过 get_publication/list_publications 读取。
    def put_publication(self, publication):
        self.state["publications"][publication["id"]] = deepcopy(publication)

    def get_visibility(self, application_id):
        return deepcopy(
            self.state["visibility"].get(
                application_id,
                {"scope": "all", "targets": [], "revision": 1},
            )
        )

    def put_visibility(self, application_id, visibility):
        self.state["visibility"][application_id] = deepcopy(visibility)

    def get_configuration(self):
        return deepcopy(self.state["configuration"])

    def put_configuration(self, configuration):
        self.state["configuration"] = deepcopy(configuration)


# 数据保存在 Python 进程内；服务重启会重置，不同后端实例之间不共享。
class InMemoryApplicationRepository:
    def __init__(self):
        self._lock = RLock()
        self._state = {
            "resources": {item["id"]: item for item in seed_applications()},
            "publications": {},
            "activities": {},
            "visibility": {},
            "configuration": default_configuration(),
        }

    @contextmanager
    def transaction(self):
        with self._lock:
            # 先在副本上处理；发生异常时不把部分修改写回正式状态。
            snapshot = deepcopy(self._state)
            yield MemorySession(snapshot)
            # 异常时不执行本行；防止审核和资源更新只完成一半。
            self._state = snapshot
