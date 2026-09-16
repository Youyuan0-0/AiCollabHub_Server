"""复用 scene_activity 记录外部发布请求；不新增表，不复制外部目录。"""

from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import insert, update

from app.modules.applications.database_values import current_time, date_value, stable_id


class DatabaseMarketActivity:
    def __init__(self, session):
        self.session = session
        self.table = session.t("activity")

    def entries(self):
        return [
            row["detail"]["marketOperation"]
            for row in self.session.rows("activity", self.table.c.detail.has_key("marketOperation"))
        ]

    def entry(self, key):
        row = self.session.one("activity", self.table.c.id == stable_id("market", key))
        return row["detail"].get("marketOperation") if row else None

    def put(self, key, record):
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
