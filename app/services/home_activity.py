"""从正式资源版本生成首页动态，运营公告与展示设置暂存应用实例内存。"""

from copy import deepcopy
from datetime import datetime
from threading import RLock
from urllib.parse import urlencode
from uuid import uuid4

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase, iso, utc_now
from app.modules.common.identity import Actor, visible
from app.modules.common.resource_database import ResourceDatabase

LABELS = {
    "dataset": "数据集",
    "model": "模型",
    "application": "场景应用",
    "knowledge": "知识库",
    "tool": "AI 工具",
}


class HomeActivityStore:
    """保存单个服务实例的公告和运营状态；进程重启后由空状态重新开始。"""

    def __init__(self) -> None:
        """创建独立的可重入锁，防止并发请求读到部分更新。"""
        self._lock = RLock()
        self._announcements: dict[str, dict] = {}
        self._settings: dict[str, bool] = {kind: True for kind in ("announcement", *LABELS)}
        self._controls: dict[tuple[str, str], dict] = {}

    def snapshot(self) -> tuple[list[dict], dict[str, bool], dict[tuple[str, str], dict]]:
        """返回公告、来源开关及单条控制的独立副本，避免调用方绕过锁修改。"""
        with self._lock:
            return (
                deepcopy(list(self._announcements.values())),
                self._settings.copy(),
                deepcopy(self._controls),
            )

    def create_announcement(self, value: dict) -> str:
        """新增公告；直接发布时由服务端记录发布时间，返回稳定编号。"""
        identifier = str(uuid4())
        now = iso(utc_now())
        record = {
            "id": identifier,
            **deepcopy(value),
            "createdAt": now,
            "publishedAt": now if value["status"] == "published" else None,
        }
        with self._lock:
            self._announcements[identifier] = record
        return identifier

    def save_announcement(self, identifier: str, value: dict) -> bool:
        """更新已有公告；撤回后保留原发布时间，再发布时刷新该时间。"""
        with self._lock:
            previous = self._announcements.get(identifier)
            if previous is None:
                return False
            published_at = previous["publishedAt"]
            if value["status"] == "published" and previous["status"] != "published":
                published_at = iso(utc_now())
            self._announcements[identifier] = {
                **previous,
                **deepcopy(value),
                "publishedAt": published_at,
            }
        return True

    def set_setting(self, kind: str, enabled: bool) -> None:
        """设置某类动态是否展示；关闭来源不会删除已有公告或版本记录。"""
        with self._lock:
            self._settings[kind] = enabled

    def set_control(self, kind: str, version_id: str, *, hidden: bool, is_pinned: bool) -> None:
        """保存单条正式版本动态的隐藏和置顶状态。"""
        with self._lock:
            self._controls[(kind, version_id)] = {"hidden": hidden, "isPinned": is_pinned}


def resource_path(kind: str, resource_id: str) -> str:
    """按现有用户端详情参数生成资源入口；工具使用独立的 resource 参数。"""
    parameter = "resource" if kind == "tool" else "resourceId"
    return f"/{RESOURCE_TABLES[kind][2]}?{urlencode({parameter: resource_id})}"


def activity_records(
    foundation: FoundationDatabase | None,
    actor: Actor,
    store: HomeActivityStore,
    *,
    include_hidden: bool = False,
    catalogs: dict[str, dict[str, dict]] | None = None,
) -> list[dict]:
    """合并内存公告与正式资源版本；资源下架或对当前用户不可见时不展示。"""
    announcements, settings, controls = store.snapshot()
    records = []
    if include_hidden or settings["announcement"]:
        records.extend(
            {
                "id": row["id"],
                "kind": "announcement",
                "title": row["title"],
                "description": row["summary"] or row["contentMarkdown"][:500],
                "time": row["publishedAt"],
                "path": None,
                "isPinned": row["isPinned"],
                "hidden": False,
            }
            for row in announcements
            if row["status"] == "published"
        )
    for kind in RESOURCE_TABLES:
        if not include_hidden and not settings[kind]:
            continue
        if catalogs is not None:
            resources = catalogs[kind].values()
        elif foundation is not None:
            with ResourceDatabase(foundation, kind).transaction(is_readonly=True) as session:
                resources = session.list_resources()
        else:
            resources = []
        for resource in resources:
            if resource["listingStatus"] != "listed" or not visible(
                actor, resource, resource["visibility"]
            ):
                continue
            versions = sorted(resource["versions"], key=lambda item: item["releasedAt"])
            for index, version in enumerate(versions):
                control = controls.get((kind, version["id"]), {})
                hidden = control.get("hidden", False)
                if hidden and not include_hidden:
                    continue
                action = "发布" if index == 0 else "更新"
                records.append(
                    {
                        "id": version["id"],
                        "kind": kind,
                        "title": f"{LABELS[kind]}「{resource['name']}」已{action}",
                        "description": (version["note"] or resource["summary"] or resource["name"])[
                            :500
                        ],
                        "time": version["releasedAt"],
                        "path": resource_path(kind, resource["id"]),
                        "isPinned": control.get("isPinned", False),
                        "hidden": hidden,
                    }
                )
    # 置顶项仍按发布时间排序；普通资料编辑不会生成新的正式版本动态。
    records.sort(
        key=lambda item: (item["isPinned"], datetime.fromisoformat(item["time"]), item["id"]),
        reverse=True,
    )
    return records[: 200 if include_hidden else 20]
