"""复用 scene_activity 记录外部发布请求；不新增表，不复制外部目录。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.modules.applications.database_session import DatabaseApplicationSession

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import insert, update

from app.modules.applications.database_values import current_time, date_value, stable_id


class DatabaseMarketActivity:
    """使用现有活动表保存外部市场操作及结果核对记录。"""

    def __init__(self, session: DatabaseApplicationSession) -> None:
        """绑定应用事务及活动表。"""
        self.session = session
        self.table = session.table("activity")

    def entries(self) -> list[dict]:
        """读取包含外部市场操作详情的活动记录。"""
        return [
            row["detail"]["marketOperation"]
            for row in self.session.rows("activity", self.table.c.detail.has_key("marketOperation"))
        ]

    def entry(self, key: str) -> dict | None:
        """按业务幂等键读取外部操作，不存在时返回空值。"""
        row = self.session.one("activity", self.table.c.id == stable_id("market", key))
        return row["detail"].get("marketOperation") if row else None

    def put(self, key: str, record: dict) -> None:
        """保存操作状态；已有终态不能被不同结果覆盖。"""
        session = self.session
        previous = self.entry(key)
        if previous and previous["status"] in ("succeeded", "failed"):
            if previous != record:
                raise HTTPException(409, "该外部操作已有最终结果，请刷新查看")
            return
        session.ensure_application(record["applicationId"])
        values = {
            "application_id": record["applicationId"],
            "operation": record["action"],
            "actor_subject": record["actorId"],
            "organization_id": record["organizationId"],
            "request_id": record["requestId"],
            "request_fingerprint": record["fingerprint"],
            "mode": "live",
            "status": record["status"],
            "provider_profile": record["profile"],
            "market_version_id": record["result"].get("defaultVersionId"),
            "external_submission_id": record["result"].get("submissionId"),
            "detail": {"marketOperation": record},
            "error_message": record["message"] if record["status"] in ("failed", "unknown") else "",
            "created_at": date_value(record["createdAt"]),
            "started_at": date_value(record["createdAt"]),
            "updated_at": current_time(),
            "deadline_at": date_value(record["createdAt"]) + timedelta(minutes=11),
            "completed_at": date_value(record["completedAt"]),
        }
        identity = stable_id("market", key)
        if previous:
            session.write(update(self.table).where(self.table.c.id == identity).values(**values))
        else:
            session.write(insert(self.table).values(id=identity, **values))
