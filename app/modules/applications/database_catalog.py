"""在一次只读事务中批量组合应用目录；缓存只存在于当前事务。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.modules.applications.database_session import DatabaseApplicationSession

from collections import defaultdict
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import bindparam, func, select, text

from app.modules.applications.database_values import iso


class DatabaseCatalogReader:
    """在只读事务中批量加载目录、权限和统计，缓存仅在当前事务有效。"""

    def __init__(self, session: DatabaseApplicationSession) -> None:
        """绑定只读事务并初始化局部目录缓存。"""
        self.session = session
        self.resources = {}
        self.visibility = {}
        self.list_ids = None

    def list_resources(self) -> list[dict]:
        """批量加载目录并返回有效资源的独立副本。"""
        if self.list_ids is None:
            rows = self.session.rows("application")
            ids = set(self.session.repository.seeds)
            ids.update(row["id"] for row in rows if row["market_listing_id"])
            self.list_ids = sorted(ids)
            self._load(ids, rows)
        return [deepcopy(self.resources[key]) for key in self.list_ids if self.resources[key]]

    def get_resource(self, application_id: str) -> dict | None:
        """读取单个资源副本，首次访问时补充当前事务缓存。"""
        self._ensure(application_id)
        return deepcopy(self.resources[application_id])

    def get_visibility(self, application_id: str) -> dict:
        """读取应用开放范围的独立副本。"""
        self._ensure(application_id)
        return deepcopy(self.visibility[application_id])

    def _ensure(self, application_id: str) -> None:
        """首次访问应用时加载当前事务内所需目录资料。"""
        if application_id not in self.resources:
            table = self.session.table("application")
            rows = self.session.rows("application", table.c.id == application_id)
            self._load({application_id}, rows)

    def _load(self, ids: Iterable[str], rows: list[dict]) -> None:
        """批量补充尚未缓存的应用、可见范围和统计数据。"""
        ids = set(ids) - self.resources.keys()
        if not ids:
            return
        session = self.session
        rows = {row["id"]: row for row in rows if row["id"] in ids}
        metrics = {
            row["application_id"]: row
            for row in session.db.execute(
                text(
                    "SELECT * FROM public.scene_application_metrics WHERE application_id IN :ids"
                ).bindparams(bindparam("ids", expanding=True)),
                {"ids": sorted(ids)},
            ).mappings()
        }
        activity = session.table("activity")
        downloads = {
            row.application_id: row
            for row in session.db.execute(
                select(
                    activity.c.application_id,
                    func.count().label("count"),
                    func.max(activity.c.completed_at).label("latest"),
                )
                .where(
                    activity.c.application_id.in_(ids),
                    activity.c.operation == "download",
                    activity.c.status == "succeeded",
                )
                .group_by(activity.c.application_id)
            )
        }
        targets = defaultdict(list)
        approved = {}
        names = {}
        if rows:
            target = session.table("visibility_target")
            for row in session.rows("visibility_target", target.c.application_id.in_(rows)):
                targets[row["application_id"]].append(row["target_id"])
            extension, note = session.table("submission_extension"), session.table("review_note")
            # 每个应用只取最近一次通过审核的密级，不逐条查询完整审核历史。
            approved = dict(
                session.db.execute(
                    select(extension.c.application_id, extension.c.security_level)
                    .join(note, note.c.submission_extension_id == extension.c.id)
                    .where(
                        extension.c.application_id.in_(rows), note.c.recommendation == "approved"
                    )
                    .distinct(extension.c.application_id)
                    .order_by(extension.c.application_id, note.c.reviewed_at.desc())
                ).all()
            )
            subjects = {row["created_by_subject"] for row in rows.values()}
            if session.repository.settings.auth_mode == "disabled":
                names = {subject: session.display_name(subject) for subject in subjects}
            else:
                names = dict(
                    session.db.execute(
                        text(
                            "SELECT DISTINCT ON (subject) subject, display_name FROM auth_session "
                            "WHERE subject IN :subjects AND display_name IS NOT NULL "
                            "ORDER BY subject, created_at DESC"
                        ).bindparams(bindparam("subjects", expanding=True)),
                        {"subjects": sorted(subjects)},
                    ).all()
                )
        for application_id in ids:
            row = rows.get(application_id)
            self.visibility[application_id] = (
                {
                    "scope": row["visibility_scope"],
                    "revision": row["visibility_revision"],
                    "targets": sorted(targets[application_id]),
                }
                if row
                else {"scope": "all", "targets": [], "revision": 1}
            )
            if row and row["market_profile"] not in (None, session.marketplace.profile):
                raise HTTPException(503, "application_marketplace_not_connected")
            resource = (
                session.marketplace.load(row["market_listing_id"], "listing")
                if row and row["market_listing_id"]
                else deepcopy(session.repository.seeds.get(application_id))
            )
            self.resources[application_id] = resource
            if resource is None:
                continue
            if row:
                resource.update(row["local_metadata"])
                resource.update(
                    createdBy=row["created_by_subject"],
                    createdByName=names.get(row["created_by_subject"], row["created_by_subject"]),
                    organizationId=row["organization_id"] or "",
                )
                if application_id in approved:
                    resource["securityLevel"] = approved[application_id]
            metric = metrics.get(application_id, {})
            download = downloads.get(application_id)
            resource.update(
                favoriteCount=int(metric.get("favorite_count", 0)),
                rating=float(metric["rating_average"]) if metric.get("rating_average") else None,
                ratingCount=int(metric.get("rating_count", 0)),
                usage=int(
                    metric.get("simulation_usage_count", 0) + metric.get("live_usage_count", 0)
                ),
                liveUsage=int(metric.get("live_usage_count", 0)),
                simulationUsage=int(metric.get("simulation_usage_count", 0)),
                downloadCount=download.count if download else 0,
                lastDownloadedAt=iso(download.latest) if download else None,
                metricsScope="platform",
            )
