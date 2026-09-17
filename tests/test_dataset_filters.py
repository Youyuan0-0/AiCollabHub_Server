from copy import deepcopy

from app.modules.common.fixtures import fixture
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets import service


def test_dataset_filters_discover_new_resources_before_paging(monkeypatch):
    """新增卡片的分类和维度无需配置；分页、筛选及零命中都保留完整选项。"""
    resources = fixture("datasets", "resources")
    added = deepcopy(resources[0])
    added.update(
        id="ds-new-type",
        category="新增分类",
        facets={"新增维度": ["新增资源", "新增资源", "", "  "], "空维度": []},
    )
    resources.append(added)
    monkeypatch.setattr(service, "fixture", lambda *_: deepcopy(resources))

    page = service.list_resources(CatalogQuery(pageSize=1))
    options = page["filterOptions"]
    assert "新增分类" in options["categories"]
    assert options["facets"]["新增维度"] == ["新增资源"]
    assert "空维度" not in options["facets"]
    match = service.list_resources(CatalogQuery(facets='{"新增维度": ["新增资源"]}'))
    assert [item["id"] for item in match["items"]] == [added["id"]]
    assert match["filterOptions"] == options
    empty = service.list_resources(CatalogQuery(favoriteIds="[]"))
    assert empty["total"] == 0
    assert empty["filterOptions"] == options


def test_empty_dataset_has_no_filter_options(monkeypatch):
    """没有卡片时不产生固定占位选项，缺少属性的卡片仍提供自身分类。"""
    monkeypatch.setattr(service, "fixture", lambda *_: [])
    assert service.list_resources(CatalogQuery())["filterOptions"] == {
        "categories": [],
        "facets": {},
    }
    resource = fixture("datasets", "resources")[0]
    resource.pop("facets", None)
    monkeypatch.setattr(service, "fixture", lambda *_: [resource])
    assert service.list_resources(CatalogQuery())["filterOptions"] == {
        "categories": [resource["category"]],
        "facets": {},
    }
