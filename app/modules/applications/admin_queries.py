from datetime import date
from typing import Literal

from pydantic import model_validator

from app.modules.common.fixtures import paginate
from app.modules.common.schemas import PageQuery


class ApplicationReviewQuery(PageQuery):
    status: Literal["", "draft", "pending", "approved", "rejected", "needs_changes"] = "pending"
    category: str = ""
    sort: Literal["updated", "submitted", "priority"] = "updated"
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        return self


def query_publications(publications: list[dict], query: ApplicationReviewQuery):
    keyword = query.q.strip().casefold()
    matched = [
        item
        for item in publications
        if (not query.category or item["data"]["category"] == query.category)
        and (
            not keyword
            or keyword
            in " ".join(
                [
                    item["id"],
                    item["applicationId"],
                    item["data"]["name"],
                    item["data"]["summary"],
                    item["data"]["owner"],
                    *item["data"]["tags"],
                ]
            ).casefold()
        )
    ]
    matched = [
        item
        for item in matched
        if (
            not query.start
            or (item["submittedAt"] and item["submittedAt"][:10] >= query.start.isoformat())
        )
        and (
            not query.end
            or (item["submittedAt"] and item["submittedAt"][:10] <= query.end.isoformat())
        )
    ]
    if query.sort == "submitted":
        matched.sort(key=lambda item: item["submittedAt"] or "", reverse=True)
    elif query.sort == "priority":
        matched.sort(
            key=lambda item: (
                {"normal": 0, "high": 1, "urgent": 2}.get(item.get("priority"), 0),
                item["submittedAt"] or "",
            ),
            reverse=True,
        )
    counts = {
        status: sum(item["status"] == status for item in matched)
        for status in ("draft", "pending", "approved", "rejected", "needs_changes")
    }
    return {
        **paginate(
            [item for item in matched if not query.status or item["status"] == query.status],
            query.page,
            query.pageSize,
        ),
        "counts": counts,
    }
