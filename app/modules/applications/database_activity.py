"""互动、任务、通知和配置到场景表的映射。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import insert, update

from app.modules.applications.database_values import current_time, date_value, iso, stable_id
from app.modules.applications.identity import ACTORS

OPERATIONS = {"runs": "run", "downloads": "download", "syncs": "sync"}


def fingerprint(value: object) -> str:
    """以稳定 JSON 表达生成内容指纹，用于幂等请求核对。"""
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DatabaseActivityMixin:
    """应用事务的互动、通知、运行和同步活动存取实现。"""

    def entries(self, kind: str) -> list[dict]:
        """按活动种类读取平台记录，并转换为现有接口结构。"""
        if kind == "market_operations":
            from app.modules.applications.database_market_activity import DatabaseMarketActivity

            return DatabaseMarketActivity(self).entries()
        if kind == "interactions":
            return [self.interaction_view(row) for row in self.rows("interaction")]
        if kind == "notifications":
            return [self.notification_view(row) for row in self.rows("notification")]
        if kind in OPERATIONS:
            return [
                self.activity_view(row)
                for row in self.rows(
                    "activity", self.table("activity").c.operation == OPERATIONS[kind]
                )
            ]
        if kind == "policy":
            return [self.configuration_record("workflow")]
        raise ValueError("Unsupported application entry kind")

    def entry(self, kind: str, key: str) -> dict | None:
        """按种类和业务键读取活动记录，缺失时返回空值。"""
        self.lock(f"{kind}:{key}")
        if kind == "review_files":
            from app.modules.applications.database_review_files import read_file

            return read_file(self, key)
        if kind == "tutorial_files":
            from app.modules.applications.database_tutorial_files import read_file

            return read_file(self, key)
        if kind == "market_lock":
            return None
        if kind == "market_operations":
            from app.modules.applications.database_market_activity import DatabaseMarketActivity

            return DatabaseMarketActivity(self).entry(key)
        if kind == "policy":
            return self.configuration_record("workflow")
        if kind == "interactions":
            actor, application_id = key.rsplit(":", 1)
            table = self.table("interaction")
            row = self.one(
                "interaction",
                table.c.actor_subject == actor,
                table.c.application_id == application_id,
            )
            return self.interaction_view(row) if row else None
        if kind == "notifications":
            table = self.table("notification")
            row = self.one("notification", table.c.id == key)
            row = row or self.one("notification", table.c.event_key == key)
            return self.notification_view(row) if row else None
        if kind == "bindings":
            profile, code = key.split(":", 1)
            table = self.table("application")
            row = self.one(
                "application", table.c.agent_profile == profile, table.c.agent_code == code
            )
            return (
                {
                    "applicationId": row["id"],
                    "createdBy": row["created_by_subject"],
                    "organizationId": row["organization_id"] or "",
                }
                if row
                else None
            )
        if kind in OPERATIONS:
            table = self.table("activity")
            row = self.one(
                "activity",
                table.c.id == stable_id(kind, key),
                table.c.operation == OPERATIONS[kind],
            )
            return self.activity_view(row) if row else None
        raise ValueError("Unsupported application entry kind")

    def put_entry(self, kind: str, key: str, value: dict) -> None:
        """按活动种类分派保存逻辑，复用当前数据库事务。"""
        self.lock(f"{kind}:{key}")
        if kind == "review_files":
            from app.modules.applications.database_review_files import save_file

            return save_file(self, value)
        if kind == "tutorial_files":
            from app.modules.applications.database_tutorial_files import insert_file

            return insert_file(self, value)
        if kind == "market_operations":
            from app.modules.applications.database_market_activity import DatabaseMarketActivity

            return DatabaseMarketActivity(self).put(key, value)
        if kind == "policy":
            return self.save_configuration("workflow", value)
        if kind == "interactions":
            return self.save_interaction(value)
        if kind == "notifications":
            return self.save_notification(key, value)
        if kind == "bindings":
            actual = self.entry("bindings", key)
            if not actual or actual["applicationId"] != value["applicationId"]:
                raise HTTPException(409, "工具链应用关联已变化")
            return None
        if kind in OPERATIONS:
            return self.save_activity(kind, key, value)
        raise ValueError("Unsupported application entry kind")

    def review_files(self, publication_id: str) -> list[dict]:
        """按申请索引读取报告，不扫描其他应用的文件。"""
        from app.modules.applications.database_review_files import file_view

        table = self.table("review_report_file")
        return [
            file_view(row)
            for row in self.rows("review_report_file", table.c.publication_id == publication_id)
        ]

    def remove_review_file(self, key: str) -> None:
        """移除未提交的报告索引，调用方已持有申请锁。"""
        from app.modules.applications.database_review_files import remove_file

        remove_file(self, key)

    @staticmethod
    def interaction_view(row: Mapping) -> dict:
        """将数据库互动记录转换为前端字段结构。"""
        return {
            "id": f"{row['actor_subject']}:{row['application_id']}",
            "actorId": row["actor_subject"],
            "applicationId": row["application_id"],
            "favorite": row["favorite"],
            "subscribed": row["follow_updates"],
            "rating": row["rating"],
            "comment": row["comment"],
            "updatedAt": iso(row["updated_at"]),
        }

    def save_interaction(self, value: dict) -> None:
        """按用户与应用保存收藏、订阅和评分状态。"""
        self.ensure_application(value["applicationId"])
        table = self.table("interaction")
        conditions = (
            table.c.application_id == value["applicationId"],
            table.c.actor_subject == value["actorId"],
        )
        previous = self.one("interaction", *conditions)
        moment = date_value(value["updatedAt"])
        values = {
            "favorite": value["favorite"],
            "follow_updates": value["subscribed"],
            "rating": value["rating"],
            "comment": value["comment"],
            "updated_at": moment,
            "favorited_at": ((previous or {}).get("favorited_at") or moment)
            if value["favorite"]
            else None,
            "followed_at": ((previous or {}).get("followed_at") or moment)
            if value["subscribed"]
            else None,
            "rated_at": moment if value["rating"] is not None else None,
        }
        if previous:
            if previous["rating"] == value["rating"] and previous["comment"] == value["comment"]:
                values["rated_at"] = previous["rated_at"]
            self.write(update(table).where(*conditions).values(**values))
        else:
            self.write(
                insert(table).values(
                    application_id=value["applicationId"], actor_subject=value["actorId"], **values
                )
            )

    @staticmethod
    def notification_view(row: Mapping) -> dict:
        """将数据库通知记录转换为接口视图。"""
        return {
            "id": row["id"],
            "recipient": row["actor_subject"],
            "applicationId": row["application_id"],
            "title": row["title"],
            "kind": row["kind"],
            "createdAt": iso(row["created_at"]),
            "read": row["read_at"] is not None,
        }

    def save_notification(self, key: str, value: dict) -> None:
        """按通知业务键保存通知或已读状态，避免重复投递。"""
        table = self.table("notification")
        existing = self.one("notification", table.c.id == key)
        if existing:
            if existing["actor_subject"] != value["recipient"]:
                raise HTTPException(404, "通知不存在")
            self.write(
                update(table)
                .where(table.c.id == key)
                .values(
                    read_at=existing["read_at"] or current_time() if value["read"] else None,
                )
            )
            return
        recipients = [value["recipient"]]
        if value["recipient"] == "@system-admin":
            settings = self.repository.settings
            recipients = (
                [actor.id for actor in ACTORS if actor.role == "admin"]
                if settings.auth_mode == "disabled"
                else settings.auth_mock_admin_subjects
            )
        for recipient in recipients:
            if self.one(
                "notification", table.c.actor_subject == recipient, table.c.event_key == key
            ):
                continue
            self.write(
                insert(table).values(
                    id=stable_id("notification", f"{recipient}:{key}"),
                    application_id=value["applicationId"],
                    actor_subject=recipient,
                    event_key=key,
                    kind=value["kind"],
                    title=value["title"],
                    created_at=date_value(value["createdAt"]),
                )
            )

    def activity_view(self, row: Mapping) -> dict:
        """组合活动记录及其关联资料，返回平台活动视图。"""
        detail = row["detail"]
        status = row["status"]
        if (
            status in ("running", "queued")
            and row["deadline_at"]
            and row["deadline_at"] < current_time()
        ):
            # 中断后不盲目重复调用；保留不确定结果，等原请求核验或用户明确发起新请求。
            # 列表按当前时间展示超时；持久化核验留在后续写事务中完成。
            if not self.is_readonly:
                self.write(
                    update(self.table("activity"))
                    .where(
                        self.table("activity").c.id == row["id"],
                        self.table("activity").c.status.in_(("running", "queued")),
                    )
                    .values(
                        status="unknown",
                        error_message="执行已中断或超时，结果尚未确认",
                        updated_at=current_time(),
                    )
                )
            status = "unknown"
            row["error_message"] = "执行已中断或超时，结果尚未确认"
        common = {
            "id": detail["clientKey"],
            "actorId": row["actor_subject"],
            "applicationId": row["application_id"],
            "fingerprint": row["request_fingerprint"],
            "status": status,
            "createdAt": iso(row["created_at"]),
        }
        if row["operation"] == "run":
            request = row["request_data"] or {}
            return {
                **common,
                "applicationName": detail["applicationName"],
                "versionId": row["market_version_id"],
                "version": detail["version"],
                "input": request.get("input", {}),
                "options": request.get("options", {}),
                "output": row["result_data"],
                "error": row["error_message"],
                "durationMs": row["duration_ms"],
                "mode": row["mode"],
                **({"completedAt": iso(row["completed_at"])} if row["completed_at"] else {}),
            }
        if row["operation"] == "download":
            return {
                "filename": detail["filename"],
                "content": self.marketplace.load(detail["downloadRef"], "download"),
                "downloadedAt": iso(row["completed_at"]),
            }
        return {
            **{key: value for key, value in common.items() if key != "applicationId"},
            "organizationId": row["organization_id"] or "",
            "source": row["provider_profile"],
            "externalId": row["agent_code"],
            "externalVersion": row["agent_version_code"],
            "publicationId": detail.get("publicationId"),
            "message": detail.get("message", ""),
            "mapping": detail.get("mapping", []),
            "workspace": row["workspace_code"],
            **({"completedAt": iso(row["completed_at"])} if row["completed_at"] else {}),
        }

    def save_activity(self, kind: str, key: str, value: dict) -> None:
        """保存运行、下载或同步记录，校验幂等键对应的请求内容。"""
        table = self.table("activity")
        identity = stable_id(kind, key)
        previous = self.one("activity", table.c.id == identity)
        app_id = value.get("applicationId")
        if kind == "syncs" and value.get("publicationId"):
            publication = self.get_publication(value["publicationId"])
            app_id = publication["applicationId"]
        if app_id:
            self.ensure_application(app_id)
        actor = value["actorId"]
        prefix = f"{actor}:" if kind == "syncs" else f"{actor}:{app_id}:"
        request_id = value.get("requestId") or key.removeprefix(prefix)
        moment = date_value(value.get("createdAt") or value.get("downloadedAt")) or current_time()
        complete = date_value(value.get("completedAt") or value.get("downloadedAt"))
        status = value.get("status", "succeeded")
        detail = {"clientKey": key}
        values = {
            "application_id": app_id,
            "operation": OPERATIONS[kind],
            "actor_subject": actor,
            "organization_id": value.get("organizationId") or None,
            "request_id": request_id,
            "request_fingerprint": fingerprint(
                value.get("fingerprint")
                or {
                    "applicationId": app_id,
                    "versionId": value.get("versionId"),
                    "mode": "simulation",
                }
            ),
            "mode": value.get("mode", "simulation"),
            "status": status,
            "created_at": moment,
            "updated_at": current_time(),
            "completed_at": complete,
            "started_at": moment,
            "deadline_at": moment + timedelta(seconds=90),
            "provider_profile": self.marketplace.profile,
            "detail": detail,
        }
        if kind == "runs":
            detail.update(applicationName=value["applicationName"], version=value["version"])
            values.update(
                request_data={"input": value["input"], "options": value["options"]},
                result_data=value["output"],
                error_message=value["error"],
                duration_ms=value["durationMs"],
                market_version_id=value["versionId"],
            )
        elif kind == "downloads":
            detail.update(
                filename=value["filename"],
                downloadRef=self.marketplace.save("download", value["content"]),
            )
        else:
            detail.update(
                publicationId=value.get("publicationId"),
                message=value["message"],
                mapping=value["mapping"],
            )
            values.update(
                provider_profile=value["source"],
                workspace_code=value.get("workspace", "demo"),
                agent_code=value["externalId"],
                agent_version_code=value["externalVersion"],
                error_message=value["message"] if status == "failed" else "",
            )
        if previous:
            if previous["status"] not in ("running", "queued"):
                raise HTTPException(409, "该请求已有结果，不能覆盖")
            # 请求身份、指纹和首次时间不随完成回写变化。
            for column in ("created_at", "started_at", "deadline_at"):
                values[column] = previous[column]
            self.write(update(table).where(table.c.id == identity).values(**values))
        else:
            self.write(insert(table).values(id=identity, **values))
