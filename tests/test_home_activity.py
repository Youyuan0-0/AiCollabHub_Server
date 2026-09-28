"""首页动态使用进程内存时的发布、筛选与隔离回归。"""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.foundation import RESOURCE_TABLES
from app.main import create_app
from app.modules.common.identity import Actor
from app.services.home_activity import HomeActivityStore, activity_records


def announcement(status: str = "published") -> dict:
    """生成公告输入，便于测试发布与撤回后的展示差异。"""
    return {
        "title": "平台公告",
        "summary": "公告摘要",
        "contentMarkdown": "公告正文",
        "status": status,
        "isPinned": True,
    }


def test_memory_activity_filters_and_resets_with_new_store():
    """公告、开关和单条控制只作用于当前实例，隐藏及下架资源不向用户展示。"""
    store = HomeActivityStore()
    announcement_id = store.create_announcement(announcement())
    first, second = str(uuid4()), str(uuid4())
    catalogs = {kind: {} for kind in RESOURCE_TABLES}
    resource_id = str(uuid4())
    catalogs["model"][resource_id] = {
        "id": resource_id,
        "name": "公开模型",
        "summary": "正式资料",
        "listingStatus": "listed",
        "visibility": {"scope": "all", "targets": []},
        "versions": [
            {"id": first, "releasedAt": "2026-09-01T10:00:00+00:00", "note": "首次发布"},
            {"id": second, "releasedAt": "2026-09-02T10:00:00+00:00", "note": "版本改进"},
        ],
    }
    actor = Actor("reader", "读者", "user", "")
    records = activity_records(None, actor, store, catalogs=catalogs)
    assert [item["id"] for item in records] == [announcement_id, second, first]
    assert records[1]["title"] == "模型「公开模型」已更新"
    assert records[1]["path"].startswith("/models?resourceId=")

    store.set_control("model", second, hidden=True, is_pinned=True)
    assert [item["id"] for item in activity_records(None, actor, store, catalogs=catalogs)] == [
        announcement_id,
        first,
    ]
    store.set_setting("announcement", False)
    assert [item["id"] for item in activity_records(None, actor, store, catalogs=catalogs)] == [
        first
    ]
    assert {
        item["id"]
        for item in activity_records(None, actor, store, include_hidden=True, catalogs=catalogs)
    } == {announcement_id, first, second}

    catalogs["model"][resource_id]["listingStatus"] = "unlisted"
    assert activity_records(None, actor, store, catalogs=catalogs) == []
    assert HomeActivityStore().snapshot() == (
        [],
        {kind: True for kind in ("announcement", *RESOURCE_TABLES)},
        {},
    )


def test_memory_announcement_status_and_snapshot_are_isolated():
    """草稿和撤回不公开，读取副本不能绕过仓库锁修改正式公告。"""
    store = HomeActivityStore()
    identifier = store.create_announcement(announcement("draft"))
    actor = Actor("reader", "读者", "user", "")
    assert activity_records(None, actor, store) == []
    assert store.save_announcement(identifier, announcement())
    rows, _, _ = store.snapshot()
    rows[0]["title"] = "外部修改"
    assert activity_records(None, actor, store)[0]["title"] == "平台公告"
    assert store.save_announcement(identifier, announcement("withdrawn"))
    assert activity_records(None, actor, store) == []
    assert not store.save_announcement(str(uuid4()), announcement())


def test_admin_announcements_survive_requests_without_database():
    """管理接口不依赖数据库；同实例保存可读取，重建实例后从空状态开始。"""
    settings = Settings(_env_file=None, app_env="development", auth_mode="disabled")
    payload = {
        "title": "本地公告",
        "summary": "摘要",
        "contentMarkdown": "正文",
        "status": "published",
        "isPinned": False,
    }
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/admin/home-activity/announcements", json=payload)
        assert response.status_code == 201
        identifier = response.json()["id"]
        listed = client.get("/api/v1/admin/home-activity")
        assert listed.status_code == 200
        assert listed.json()["announcements"][0]["id"] == identifier
        assert listed.json()["events"][0]["title"] == "本地公告"
        withdrawn = client.put(
            f"/api/v1/admin/home-activity/announcements/{identifier}",
            json={**payload, "status": "withdrawn"},
        )
        assert withdrawn.status_code == 200
        assert client.get("/api/v1/admin/home-activity").json()["events"] == []
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/admin/home-activity").json()["announcements"] == []
