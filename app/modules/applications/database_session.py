"""平台表的读写和外部模拟资料组合；业务表只保存允许的本地字段。"""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import delete, insert, select, text, update

from app.modules.applications.database_activity import DatabaseActivityMixin
from app.modules.applications.database_catalog import DatabaseCatalogReader
from app.modules.applications.database_publications import DatabasePublicationMixin
from app.modules.applications.database_values import current_time, date_value, iso
from app.modules.applications.demo_marketplace import LOCAL_FIELDS
from app.modules.applications.identity import ACTORS, DEFAULT_USER
from app.modules.applications.publication_schemas import APPLICATION_CATEGORIES


class DatabaseApplicationSession(DatabaseActivityMixin, DatabasePublicationMixin):
    def __init__(self, repository, database, tables, *, readonly=False):
        self.repository = repository
        self.db = database
        self.tables = tables
        self.marketplace = repository.marketplace
        self.dirty = False
        self._locks = set()
        self.readonly = readonly
        self.catalog = DatabaseCatalogReader(self) if readonly else None

    def t(self, name):
        return self.tables["scene_" + name]

    def rows(self, name, *conditions):
        return [
            dict(row) for row in self.db.execute(select(self.t(name)).where(*conditions)).mappings()
        ]

    def one(self, name, *conditions):
        row = self.db.execute(select(self.t(name)).where(*conditions)).mappings().first()
        return dict(row) if row else None

    def write(self, statement):
        if self.readonly:
            raise RuntimeError("Cannot write in an application read transaction")
        result = self.db.execute(statement)
        self.dirty = True
        return result

    def lock(self, key):
        if self.readonly:
            return
        # 对缺失记录同样加锁，避免两次首次收藏/同一请求同时插入；不同应用可独立操作。
        if key not in self._locks:
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "scene:" + key},
            )
            self._locks.add(key)

    def display_name(self, subject):
        if self.repository.settings.auth_mode == "disabled":
            return next((actor.name for actor in ACTORS if actor.id == subject), subject)
        # 只借用公共认证模块的显示资料，不以会话存在与否建立业务归属或授权关系。
        return (
            self.db.scalar(
                text(
                    "SELECT display_name FROM auth_session WHERE subject=:subject "
                    "AND display_name IS NOT NULL ORDER BY created_at DESC LIMIT 1"
                ),
                {"subject": subject},
            )
            or subject
        )

    def ensure_application(self, application_id, resource=None, *, published=True):
        self.lock("application:" + application_id)
        table = self.t("application")
        row = self.one("application", table.c.id == application_id)
        if row:
            if row["market_profile"] not in (None, self.marketplace.profile):
                raise HTTPException(503, "application_marketplace_not_connected")
            return row
        resource = resource or self.repository.seeds.get(application_id)
        if resource is None:
            raise HTTPException(404, "应用不存在")
        values = {
            "id": application_id,
            "created_by_subject": resource.get("createdBy", DEFAULT_USER.id),
            "organization_id": resource.get("organizationId", "org-research") or None,
            "market_profile": self.marketplace.profile,
            "market_resource_id": "demo-resource:" + application_id,
            "market_listing_id": self.marketplace.save_listing(resource) if published else None,
            "local_metadata": {
                key: deepcopy(resource[key]) for key in LOCAL_FIELDS if key in resource
            }
            if published
            else {},
            "created_at": date_value(resource.get("createdAt")) or current_time(),
        }
        self.write(insert(table).values(**values))
        return self.one("application", table.c.id == application_id)

    def list_resources(self):
        if self.catalog is not None:
            return self.catalog.list_resources()
        ids = set(self.repository.seeds)
        ids.update(row["id"] for row in self.rows("application") if row["market_listing_id"])
        return [resource for key in sorted(ids) if (resource := self.get_resource(key))]

    def get_resource(self, application_id):
        if self.catalog is not None:
            return self.catalog.get_resource(application_id)
        self.lock("application:" + application_id)
        table = self.t("application")
        row = self.one("application", table.c.id == application_id)
        if row and row["market_profile"] not in (None, self.marketplace.profile):
            raise HTTPException(503, "application_marketplace_not_connected")
        if row and row["market_listing_id"]:
            resource = self.marketplace.load(row["market_listing_id"], "listing")
        else:
            resource = deepcopy(self.repository.seeds.get(application_id))
        if resource is None:
            return None
        if row:
            resource.update(row["local_metadata"])
            resource.update(
                createdBy=row["created_by_subject"],
                createdByName=self.display_name(row["created_by_subject"]),
                organizationId=row["organization_id"] or "",
            )
            extension = self.t("submission_extension")
            note = self.t("review_note")
            approved = self.db.execute(
                select(extension.c.security_level)
                .join(note, note.c.submission_extension_id == extension.c.id)
                .where(
                    extension.c.application_id == application_id,
                    note.c.recommendation == "approved",
                )
                .order_by(note.c.reviewed_at.desc())
            ).first()
            if approved:
                resource["securityLevel"] = approved[0]
        metrics = (
            self.db.execute(
                text(
                    "SELECT * FROM public.scene_application_metrics "
                    "WHERE application_id=:application_id"
                ),
                {"application_id": application_id},
            )
            .mappings()
            .first()
        )
        resource.update(
            favoriteCount=int(metrics["favorite_count"]) if metrics else 0,
            rating=float(metrics["rating_average"])
            if metrics and metrics["rating_average"]
            else None,
            ratingCount=int(metrics["rating_count"]) if metrics else 0,
            usage=int(metrics["simulation_usage_count"] + metrics["live_usage_count"])
            if metrics
            else 0,
            liveUsage=int(metrics["live_usage_count"]) if metrics else 0,
            simulationUsage=int(metrics["simulation_usage_count"]) if metrics else 0,
        )
        activity = self.t("activity")
        downloads = self.rows(
            "activity",
            activity.c.application_id == application_id,
            activity.c.operation == "download",
            activity.c.status == "succeeded",
        )
        resource.update(
            downloadCount=len(downloads),
            lastDownloadedAt=max((iso(item["completed_at"]) for item in downloads), default=None),
            metricsScope="platform",
        )
        return resource

    def put_resource(self, resource):
        row = self.ensure_application(resource["id"], resource)
        table = self.t("application")
        self.write(
            update(table)
            .where(table.c.id == resource["id"])
            .values(
                market_listing_id=self.marketplace.save_listing(resource),
                local_metadata={
                    key: deepcopy(resource[key]) for key in LOCAL_FIELDS if key in resource
                },
                revision=row["revision"] + 1,
                updated_at=current_time(),
            )
        )

    def get_visibility(self, application_id):
        if self.catalog is not None:
            return self.catalog.get_visibility(application_id)
        self.lock("application:" + application_id)
        row = self.one("application", self.t("application").c.id == application_id)
        if row is None:
            return {"scope": "all", "targets": [], "revision": 1}
        targets = self.rows(
            "visibility_target", self.t("visibility_target").c.application_id == application_id
        )
        return {
            "scope": row["visibility_scope"],
            "revision": row["visibility_revision"],
            "targets": sorted(item["target_id"] for item in targets),
        }

    def put_visibility(self, application_id, visibility):
        self.ensure_application(application_id)
        application, targets = self.t("application"), self.t("visibility_target")
        self.write(delete(targets).where(targets.c.application_id == application_id))
        self.write(
            update(application)
            .where(application.c.id == application_id)
            .values(
                visibility_scope=visibility["scope"],
                visibility_revision=visibility["revision"],
                updated_at=current_time(),
            )
        )
        for target in visibility["targets"]:
            self.write(
                insert(targets).values(
                    application_id=application_id, scope=visibility["scope"], target_id=target
                )
            )

    def get_configuration(self):
        return self.configuration_record("form")

    def configuration_record(self, key):
        self.lock("configuration:" + key)
        row = self.one("configuration", self.t("configuration").c.key == key)
        if row is None:
            raise HTTPException(503, "application_configuration_not_initialized")
        value = deepcopy(row["value"])
        if key == "workflow":
            categories = {
                f"demo-category-{i}": name for i, name in enumerate(APPLICATION_CATEGORIES)
            }
            refs = value.pop("hotCategoryRefs", [])
            value["hotCategories"] = [
                categories[item["categoryId"]]
                for item in refs
                if item.get("marketProfile") == self.marketplace.profile
                and item.get("categoryId") in categories
            ]
        return {**value, "revision": row["revision"], "updatedAt": iso(row["updated_at"])}

    def put_configuration(self, configuration):
        self.save_configuration("form", configuration)

    def save_configuration(self, key, configuration):
        self.lock("configuration:" + key)
        values = {
            name: deepcopy(value)
            for name, value in configuration.items()
            if name not in ("revision", "updatedAt", "updatedBy")
        }
        if key == "workflow":
            names = {name: f"demo-category-{i}" for i, name in enumerate(APPLICATION_CATEGORIES)}
            values["hotCategoryRefs"] = [
                {"marketProfile": self.marketplace.profile, "categoryId": names[name]}
                for name in values.pop("hotCategories", [])
            ]
        table = self.t("configuration")
        result = self.write(
            update(table)
            .where(table.c.key == key, table.c.revision == configuration["revision"] - 1)
            .values(
                value=values,
                revision=configuration["revision"],
                updated_at=current_time(),
                updated_by_subject=configuration.get("updatedBy"),
            )
        )
        if result.rowcount != 1:
            raise HTTPException(409, "配置已发生变化，请重新加载")
