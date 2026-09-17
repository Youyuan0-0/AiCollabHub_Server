"""验证实际快照缺失或损坏时，目录可继续读取且详情不会伪装成功。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, MetaData, String, Table

from app.modules.applications.database_catalog import DatabaseCatalogReader
from app.modules.applications.demo_marketplace import DemoMarketplaceStore


@pytest.fixture
def catalog(tmp_path):
    """隔离数据库查询结果，使用真实文件存储验证快照读取边界。"""
    store = DemoMarketplaceStore(tmp_path)
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, *(Column(column, String) for column in columns))
        for name, columns in {
            "activity": ["application_id", "operation", "status", "completed_at"],
            "visibility_target": ["application_id"],
            "submission_extension": ["id", "application_id", "security_level"],
            "review_note": ["submission_extension_id", "recommendation", "reviewed_at"],
        }.items()
    }
    resources = [{"id": "available", "name": "完整应用"}, {"id": "missing", "name": "原始应用"}]
    rows = [
        {
            "id": resource["id"],
            "market_profile": store.profile,
            "market_listing_id": store.save_listing(resource),
            "visibility_scope": "all",
            "visibility_revision": 1,
            "local_metadata": {},
            "created_by_subject": "demo-reader",
            "organization_id": "org-test",
        }
        for resource in resources
    ]
    database = MagicMock()
    database.execute.return_value.mappings.return_value = []
    database.execute.return_value.all.return_value = []
    session = SimpleNamespace(
        marketplace=store,
        repository=SimpleNamespace(
            # 即使存在同编号样例，也不能用样例替代缺失的已发布快照。
            seeds={"missing": {"id": "missing", "name": "旧样例"}},
            settings=SimpleNamespace(auth_mode="disabled"),
        ),
        db=database,
        table=lambda name: tables[name],
        rows=lambda name, *conditions: rows if name == "application" else [],
        display_name=lambda subject: subject,
        resource_warnings=[],
    )
    return session, rows


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_incomplete_snapshot_keeps_other_resources_and_reports_warning(catalog, failure):
    session, rows = catalog
    reference = rows[1]["market_listing_id"]
    snapshot = session.marketplace.directory / "listing" / f"{reference}.json"
    if failure == "missing":
        snapshot.unlink()
    else:
        snapshot.write_text('{"kind":"listing","data":{}}', encoding="utf-8")
    reader = DatabaseCatalogReader(session)
    assert [item["id"] for item in reader.list_resources()] == ["available"]
    assert len(session.resource_warnings) == 1
    assert reader.get_resource("available")["name"] == "完整应用"
    with pytest.raises(HTTPException) as raised:
        reader.get_resource("missing")
    assert raised.value.status_code == 503
    assert raised.value.detail == "application_demo_snapshot_unavailable"
    assert rows[1]["market_listing_id"] == reference


def test_catalog_does_not_swallow_other_service_errors(catalog, monkeypatch):
    session, _ = catalog

    def unavailable(*args):
        raise HTTPException(503, "application_marketplace_not_connected")

    monkeypatch.setattr(session.marketplace, "load", unavailable)
    with pytest.raises(HTTPException) as raised:
        DatabaseCatalogReader(session).list_resources()
    assert raised.value.detail == "application_marketplace_not_connected"
    assert session.resource_warnings == []
