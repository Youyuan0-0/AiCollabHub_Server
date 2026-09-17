from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from app.modules.common.fixtures import fixture, paginate
from app.modules.common.schemas import CatalogQuery

MODULES = ("datasets", "models", "applications", "tools")


def last_download_timestamp(resource: dict) -> float:
    """读取最近下载时间用于排序，缺失或无效时间排在最后。"""
    value = resource.get("lastDownloadedAt")
    if not value:
        return float("-inf")
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.timestamp()
    except (ValueError, TypeError):
        return float("-inf")


def list_samples(module: str, query: CatalogQuery) -> dict:
    """读取指定模块的固定样例，再按目录查询条件筛选。"""
    resources = fixture(module, "resources")
    return query_resources(resources, query)


def query_resources(resources: list[dict], query: CatalogQuery) -> dict:
    """筛选、排序并分页返回资源；统计值相同时使用稳定的编号顺序。"""
    facets, favorites = query.filters()
    keyword = query.q.strip().casefold()
    items = [
        resource
        for resource in resources
        if (
            (
                not keyword
                or keyword
                in " ".join(
                    [
                        resource["name"],
                        resource["description"],
                        resource["owner"],
                        resource["category"],
                        *resource["tags"],
                    ]
                ).casefold()
            )
            and (not query.category or query.category == resource["category"])
            and (not query.owner or query.owner == resource["owner"])
            and (favorites is None or resource["id"] in favorites)
            and all(
                not values or set(values).intersection(resource.get("facets", {}).get(key, []))
                for key, values in facets.items()
            )
        )
    ]
    keys = {
        "latest": lambda resource: resource["updatedAt"],
        "rating": lambda resource: resource["rating"] or 0,
        "popular": lambda resource: resource["usage"],
        "recommended": lambda resource: (bool(resource.get("badge")), resource["usage"]),
        "recent_download": last_download_timestamp,
        "downloads": lambda resource: resource.get("downloadCount") or 0,
        "favorites": lambda resource: resource.get("favoriteCount") or 0,
    }
    if query.sort in ("rating", "recent_download", "downloads", "favorites"):
        # 相同统计值按编号固定顺序，翻页时不会因并列指标交换位置。
        items.sort(key=lambda resource: resource["id"])
    items.sort(key=keys[query.sort], reverse=True)
    return {
        **paginate(items, query.page, query.page_size),
        "owners": list(dict.fromkeys(resource["owner"] for resource in resources)),
    }


def find_sample(resource_id: str) -> dict:
    """跨资源模块查找固定样例，未找到时返回 404。"""
    for module in MODULES:
        for resource in fixture(module, "resources"):
            if resource["id"] == resource_id:
                return resource
    raise HTTPException(404, "sample_resource_not_found")
