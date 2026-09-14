from fastapi import HTTPException

from app.modules.common.fixtures import fixture, paginate
from app.modules.common.schemas import CatalogQuery

MODULES = ("datasets", "models", "applications", "tools")


def list_samples(module: str, query: CatalogQuery):
    resources = fixture(module, "resources")
    facets, favorites = query.filters()
    keyword = query.q.strip().casefold()
    items = [
        r
        for r in resources
        if (
            (
                not keyword
                or keyword
                in " ".join(
                    [r["name"], r["description"], r["owner"], r["category"], *r["tags"]]
                ).casefold()
            )
            and (not query.category or query.category == r["category"])
            and (not query.owner or query.owner == r["owner"])
            and (favorites is None or r["id"] in favorites)
            and all(
                not values or set(values).intersection(r.get("facets", {}).get(key, []))
                for key, values in facets.items()
            )
        )
    ]
    keys = {
        "latest": lambda r: r["updatedAt"],
        "rating": lambda r: r["rating"],
        "popular": lambda r: r["usage"],
        "recommended": lambda r: (bool(r.get("badge")), r["usage"]),
    }
    items.sort(key=keys[query.sort], reverse=True)
    return {
        **paginate(items, query.page, query.pageSize),
        "owners": list(dict.fromkeys(r["owner"] for r in resources)),
    }


def find_sample(resource_id: str):
    for module in MODULES:
        for resource in fixture(module, "resources"):
            if resource["id"] == resource_id:
                return resource
    raise HTTPException(404, "sample_resource_not_found")
