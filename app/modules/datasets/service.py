from __future__ import annotations

from app.modules.common.catalog import query_resources
from app.modules.common.fixtures import fixture
from app.modules.common.schemas import CatalogQuery


def list_resources(query: CatalogQuery, resources: list[dict] | None = None) -> dict:
    """按 query 筛选数据集，返回分页卡片及完整目录的动态筛选选项。"""
    # 未显式传入目录时读取样例；空目录不能回退为样例，避免数据清空后重新出现。
    resources = fixture("datasets", "resources") if resources is None else resources
    categories: dict[str, None] = {}
    facets: dict[str, dict[str, None]] = {}
    # 在筛选与分页之前汇总，避免翻页或零命中时丢失选项；按出现顺序去重并忽略空值。
    for resource in resources:
        category = resource.get("category")
        if category and category.strip():
            categories[category] = None
        for key, values in (resource.get("facets") or {}).items():
            for value in values:
                if value and value.strip():
                    facets.setdefault(key, {})[value] = None
    return {
        **query_resources(resources, query),
        "filterOptions": {
            "categories": list(categories),
            "facets": {key: list(values) for key, values in facets.items()},
        },
    }
