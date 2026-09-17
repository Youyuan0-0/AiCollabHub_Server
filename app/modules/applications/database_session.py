"""平台表的读写和外部模拟资料组合；业务表只保存允许的本地字段。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Table
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.elements import ColumnElement

if TYPE_CHECKING:
    from app.modules.applications.database_repository import DatabaseApplicationRepository

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
from app.modules.common.auth.models import User


class DatabaseApplicationSession(DatabaseActivityMixin, DatabasePublicationMixin):
    """组合平台业务表和供应方资料的事务入口，只保存允许落库的本地字段。"""

    def __init__(
        self,
        repository: DatabaseApplicationRepository,
        database: Session,
        tables: dict[str, Table],
        *,
        is_readonly: bool = False,
    ) -> None:
        """绑定当前数据库事务和表映射，只读事务使用目录批量读取器。"""
        self.repository = repository
        self.db = database
        self.tables = tables
        self.marketplace = repository.marketplace
        self.is_dirty = False
        self._locks = set()
        self.is_readonly = is_readonly
        self.catalog = DatabaseCatalogReader(self) if is_readonly else None

    def table(self, name: str) -> Table:
        """按业务表短名取得带 scene_ 前缀的已反射表。"""
        return self.tables["scene_" + name]

    def rows(self, name: str, *conditions: ColumnElement[bool]) -> list[dict]:
        """按条件读取业务表，返回行字典列表。"""
        return [
            dict(row)
            for row in self.db.execute(select(self.table(name)).where(*conditions)).mappings()
        ]

    def one(self, name: str, *conditions: ColumnElement[bool]) -> dict | None:
        """按条件读取首行字典，不存在时返回空值。"""
        row = self.db.execute(select(self.table(name)).where(*conditions)).mappings().first()
        return dict(row) if row else None

    def write(self, statement: Delete | Insert | Update) -> CursorResult:
        """执行当前事务的增删改语句，并标记需要提交；只读事务拒绝写入。"""
        if self.is_readonly:
            raise RuntimeError("Cannot write in an application read transaction")
        result = self.db.execute(statement)
        self.is_dirty = True
        return result

    def lock(self, key: str) -> None:
        """对业务键获取事务级咨询锁，协调首次创建记录的并发操作。"""
        if self.is_readonly:
            return
        # 对缺失记录同样加锁，避免两次首次收藏/同一请求同时插入；不同应用可独立操作。
        if key not in self._locks:
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": "scene:" + key},
            )
            self._locks.add(key)

    def display_name(self, subject: str) -> str:
        """读取身份显示名，缺失时使用身份编号，不据此推断授权。"""
        if self.repository.settings.auth_mode == "disabled":
            return next((actor.name for actor in ACTORS if actor.id == subject), subject)
        # 显示资料从稳定用户档案读取，不随登录会话到期或清理而丢失。
        return (
            self.db.scalar(select(User.display_name).where(User.iam_subject == subject)) or subject
        )

    def ensure_application(
        self, application_id: str, resource: dict | None = None, *, published: bool = True
    ) -> dict | None:
        """锁定并取得应用基础记录，缺失时按样例或传入资源初始化。"""
        self.lock("application:" + application_id)
        table = self.table("application")
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

    def list_resources(self) -> list[dict]:
        """合并本地目录和供应方资料，返回已发布资源列表。"""
        if self.catalog is not None:
            return self.catalog.list_resources()
        ids = set(self.repository.seeds)
        ids.update(row["id"] for row in self.rows("application") if row["market_listing_id"])
        return [resource for key in sorted(ids) if (resource := self.get_resource(key))]

    def get_resource(self, application_id: str) -> dict | None:
        """读取应用详情并组合平台权限、密级和使用统计。"""
        if self.catalog is not None:
            return self.catalog.get_resource(application_id)
        self.lock("application:" + application_id)
        table = self.table("application")
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
            extension = self.table("submission_extension")
            note = self.table("review_note")
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
        activity = self.table("activity")
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

    def put_resource(self, resource: dict) -> None:
        """保存资源的供应方引用及允许落库的本地元数据。"""
        row = self.ensure_application(resource["id"], resource)
        table = self.table("application")
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

    def get_visibility(self, application_id: str) -> dict:
        """读取开放范围和目标列表，缺失时使用默认配置。"""
        if self.catalog is not None:
            return self.catalog.get_visibility(application_id)
        self.lock("application:" + application_id)
        row = self.one("application", self.table("application").c.id == application_id)
        if row is None:
            return {"scope": "all", "targets": [], "revision": 1}
        targets = self.rows(
            "visibility_target", self.table("visibility_target").c.application_id == application_id
        )
        return {
            "scope": row["visibility_scope"],
            "revision": row["visibility_revision"],
            "targets": sorted(item["target_id"] for item in targets),
        }

    def put_visibility(self, application_id: str, visibility: dict) -> None:
        """在当前事务中替换应用的开放范围和目标列表。"""
        self.ensure_application(application_id)
        application, targets = self.table("application"), self.table("visibility_target")
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

    def get_configuration(self) -> dict:
        """读取发布表单配置。"""
        return self.configuration_record("form")

    def configuration_record(self, key: str) -> dict:
        """读取并锁定配置记录，将供应方分类引用转换为平台分类名称。"""
        self.lock("configuration:" + key)
        row = self.one("configuration", self.table("configuration").c.key == key)
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

    def put_configuration(self, configuration: dict) -> None:
        """保存发布表单配置并检查配置版本。"""
        self.save_configuration("form", configuration)

    def save_configuration(self, key: str, configuration: dict) -> None:
        """按 revision 更新配置，将平台分类名称转换为供应方引用。"""
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
        table = self.table("configuration")
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
