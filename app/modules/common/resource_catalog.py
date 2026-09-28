"""正式资源、历史版本及数据库明细统计的投影，不依赖演示文件。"""

import re
from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import Table, func, insert, select, update

from app.db.foundation import date_value, identifier, iso, utc_now


def count_value(value: str | int | None, *, binary: bool = False) -> int | None:
    """将明确的规模单位转为整数；混合描述保留在版本资料中，不推测数字。"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMB]?)\s*(?:tokens?)?\s*", str(value or ""), re.I)
    if not match:
        return None
    base = 1024 if binary else 1000
    amount = Decimal(match[1]) * base ** {"": 0, "K": 1, "M": 2, "B": 3}[match[2].upper()]
    return int(amount) if amount == int(amount) and 0 < amount < 2**63 else None


def review_quality_score(scores: dict | None) -> float | None:
    """按审核维度计算五分制质量分；缺少有效评分时不推测结果。"""
    if not scores:
        return None
    values = [
        float(value)
        for value in scores.values()
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
    ]
    return round(sum(values) / len(values), 2) if values else None


class ResourceCatalog:
    """资源事务内的目录读写，互动统计只从共用明细汇总。"""

    def main_values(self, resource: dict, *, creator_id: UUID | None = None) -> dict:
        """只映射已确认的概览字段；表单专属资料由正式版本快照保存。"""
        values = {
            "name": resource["name"],
            "english_name": resource.get("englishName") or None,
            "summary": resource.get("summary") or resource.get("description", ""),
            "description_markdown": resource.get("descriptionMarkdown") or "",
            "provider_name": resource.get("owner") or resource.get("providerName", ""),
            "contact": resource.get("contact") or None,
            "tags": list(dict.fromkeys(resource.get("tags", []))),
            "updated_at": utc_now(),
        }
        if creator_id is not None:
            values["creator_id"] = creator_id
        if self.kind == "dataset":
            values.update(
                ai_purpose=resource.get("aiPurpose", ""),
                task_types=resource.get("taskTypes", resource.get("tasks", [])),
                data_applications=resource.get("dataApplication", []),
                data_shapes=resource.get("dataShape", []),
                update_frequency=resource.get("updateFrequency") or None,
                record_count=resource.get("recordCount"),
                data_size_bytes=resource.get("dataSizeBytes"),
            )
        elif self.kind == "application":
            values.update(
                category=resource.get("category", ""),
                capabilities=resource.get("capabilities", []),
                development_team=resource.get("team") or resource.get("developmentTeam"),
                sharing_agreement=resource.get("sharingAgreement") or None,
            )
        elif self.kind == "model":
            values.update(
                architecture=resource.get("architecture") or None,
                parameter_count=resource.get("parameterCount")
                or count_value(resource.get("parameters")),
                context_length_tokens=resource.get("contextLengthTokens")
                or count_value(resource.get("contextLength"), binary=True),
                license=resource.get("license") or None,
                task_types=[resource["taskType"]] if resource.get("taskType") else [],
                frameworks=[resource["framework"]] if resource.get("framework") else [],
                weight_formats=[resource["modelFormat"]] if resource.get("modelFormat") else [],
                precisions=[resource["precision"]] if resource.get("precision") else [],
                capabilities=resource.get("capabilities", []),
            )
        elif self.kind == "knowledge":
            values.update(
                category=resource.get("category") or None,
                knowledge_type=resource.get("knowledgeType") or "document",
                security_level=resource.get("securityLevel") or resource.get("dataLevel"),
            )
        else:
            source = resource.get("sourceType", "self_developed")
            values.update(
                category=resource.get("category", ""),
                source_type="third_party"
                if source in {"third_party", "第三方", "第三方工具", "第三方接入", "开源集成"}
                else "self_developed",
                development_team=resource.get("developmentTeam") or resource.get("team"),
                license=resource.get("license") or None,
                capabilities=resource.get("capabilities", []),
            )
        return values

    def list_resources(self) -> list[dict]:
        """只返回已经形成正式版本的资源，不从 Mock 样例补充数据库目录。"""
        main = self.table(self.main_name)
        rows = list(
            self.db.execute(
                select(main).where(main.c.current_version_id.is_not(None)).order_by(main.c.id)
            ).mappings()
        )
        if rows and self.is_readonly:
            self.prefetch_catalog(rows)
        return [self.resource_view(dict(row)) for row in rows]

    def prefetch_catalog(self, rows: list) -> None:
        """在当前只读事务中批量读取版本、作者、统计和可见范围。

        结果仅缓存在当前仓储工作单元，避免目录每增加一条资源就增加一组查询。
        这些数据依赖当前事务快照，不能挪成全局缓存，否则可能继续展示旧权限或统计。
        """
        ids = [row["id"] for row in rows]
        versions = self.table(self.prefix + "_versions")
        self._catalog_versions = {key: [] for key in ids}
        for row in self.db.execute(
            select(versions)
            .where(versions.c[self.prefix + "_id"].in_(ids))
            .order_by(versions.c.published_at.desc(), versions.c.id)
        ).mappings():
            self._catalog_versions[row[self.prefix + "_id"]].append(row)
        publication_ids = {
            version["publication_id"]
            for history in self._catalog_versions.values()
            for version in history
        }
        self._catalog_quality_scores = {key: None for key in publication_ids}
        if publication_ids:
            reviews = self.table("resource_reviews")
            for row in self.db.execute(
                select(reviews.c.publication_id, reviews.c.quality_scores)
                .where(
                    reviews.c.resource_type == self.kind,
                    reviews.c.publication_id.in_(publication_ids),
                    reviews.c.decision == "approved",
                    reviews.c.quality_scores.is_not(None),
                )
                .order_by(reviews.c.created_at.desc(), reviews.c.id)
            ).mappings():
                if self._catalog_quality_scores[row["publication_id"]] is None:
                    self._catalog_quality_scores[row["publication_id"]] = review_quality_score(
                        row["quality_scores"]
                    )
        users = self.table("users")
        for row in self.db.execute(
            select(users).where(
                users.c.id.in_({row["creator_id"] for row in rows if row["creator_id"]})
            )
        ).mappings():
            self.db.info.setdefault("user_views", {})[row["id"]] = {
                "id": row["iam_subject"],
                "name": row["display_name"] or row["iam_subject"],
            }
        self._catalog_metrics = {
            key: {
                "favoriteCount": 0,
                "rating": None,
                "ratingCount": 0,
                "downloadCount": 0,
                "lastDownloadedAt": None,
                "usage": 0,
                "metricsScope": "platform",
            }
            for key in ids
        }
        for name in ("resource_favorites", "resource_ratings", "resource_downloads"):
            table = self.table(name)
            columns = [table.c.resource_id, func.count().label("count")]
            if name == "resource_ratings":
                columns.append(func.avg(table.c.rating).label("average"))
            if name == "resource_downloads":
                columns.append(func.max(table.c.completed_at).label("latest"))
            query = select(*columns).where(
                table.c.resource_type == self.kind, table.c.resource_id.in_(ids)
            )
            if name == "resource_downloads":
                query = query.where(table.c.status == "completed")
            for row in self.db.execute(query.group_by(table.c.resource_id)).mappings():
                metrics = self._catalog_metrics[row["resource_id"]]
                if name == "resource_favorites":
                    metrics["favoriteCount"] = row["count"]
                elif name == "resource_ratings":
                    metrics.update(rating=round(float(row["average"]), 2), ratingCount=row["count"])
                else:
                    metrics.update(downloadCount=row["count"], lastDownloadedAt=iso(row["latest"]))
        logs = self.table("operation_logs")
        for target_id, count in self.db.execute(
            select(logs.c.target_id, func.count())
            .where(
                logs.c.target_type == {"knowledge": "knowledge_base"}.get(self.kind, self.kind),
                logs.c.action == "invoke",
                logs.c.target_id.in_([str(key) for key in ids]),
            )
            .group_by(logs.c.target_id)
        ):
            self._catalog_metrics[identifier(target_id)]["usage"] = count
        policies, targets = (
            self.table("resource_visibility_policies"),
            self.table("resource_visibility_targets"),
        )
        self._catalog_visibility = {
            str(key): {"scope": "all", "targets": [], "revision": 1} for key in ids
        }
        for row in self.db.execute(
            select(policies, users.c.iam_subject)
            .outerjoin(targets, targets.c.policy_id == policies.c.id)
            .outerjoin(users, users.c.id == targets.c.user_id)
            .where(policies.c.resource_type == self.kind, policies.c.resource_id.in_(ids))
            .order_by(users.c.iam_subject)
        ).mappings():
            value = self._catalog_visibility[str(row["resource_id"])]
            value.update(scope=row["scope"], revision=row["revision"])
            if row["scope"] in {"organizations", "projects"}:
                value["targets"] = list(row["directory_targets"])
            elif row["iam_subject"]:
                value["targets"].append(row["iam_subject"])

    def get_resource(self, resource_id: str) -> dict | None:
        """查询稳定 UUID 对应的正式资料；未发布申请没有公开主表详情。"""
        self.lock("resource:" + resource_id)
        main = self.table(self.main_name)
        row = (
            self.db.execute(select(main).where(main.c.id == identifier(resource_id)))
            .mappings()
            .first()
        )
        if row is None or row["current_version_id"] is None:
            return None
        return self.resource_view(dict(row))

    def resource_view(self, row: dict) -> dict:
        """从主表、版本及有效互动生成原有接口字段，统计不回写资源。"""
        versions = self.table(self.prefix + "_versions")
        history = getattr(self, "_catalog_versions", {}).get(row["id"])
        if history is None:
            history = list(
                self.db.execute(
                    select(versions)
                    .where(versions.c[self.prefix + "_id"] == row["id"])
                    .order_by(versions.c.published_at.desc(), versions.c.id)
                ).mappings()
            )
        # 展示基线由主表的当前版本外键确定，不能假定按发布时间排序的第一条就是当前版。
        current = next(item for item in history if item["id"] == row["current_version_id"])
        resource = deepcopy(current["snapshot"])
        context = resource.pop("publicationContext", {})
        actor = (
            self.foundation.user(self.db, row["creator_id"])
            if row["creator_id"]
            else {"id": "", "name": ""}
        )
        resource.update(
            id=str(row["id"]),
            kind=self.kind,
            name=row["name"],
            englishName=row["english_name"] or "",
            summary=row["summary"],
            description=row["summary"],
            descriptionMarkdown=row["description_markdown"] or "",
            owner=row["provider_name"] or "",
            contact=row["contact"] or "",
            tags=row["tags"],
            createdBy=actor["id"],
            createdByName=actor["name"],
            organizationId=context.get("organizationId", ""),
            source=context.get("source", {"kind": "manual"}),
            securityLevel=context.get("securityLevel", "internal"),
            createdAt=iso(row["created_at"]),
            updatedAt=iso(row["updated_at"]),
            version=current["version"],
            versionId=str(current["id"]),
            revision=int(row["updated_at"].timestamp() * 1_000_000),
            listingStatus="listed" if row["status"] == "enabled" else "unlisted",
            status="available" if row["status"] == "enabled" else "disabled",
            publicationStatus="approved",
            approvedAt=iso(current["published_at"]),
            tone="blue",
            serviceStatus="not_connected",
            versions=[
                {
                    "id": str(item["id"]),
                    "version": item["version"],
                    "releasedAt": iso(item["published_at"]),
                    "note": item["version_notes"] or "",
                    "status": "current" if item["id"] == current["id"] else "available",
                }
                for item in history
            ],
        )
        for key in ("owner", "contact", "baseVersion"):
            if key in context and not resource.get(key):
                resource[key] = context[key]
        resource["visibility"] = self.get_visibility(str(row["id"]))
        resource.update(self.resource_metrics(row["id"]))
        if self.kind == "model":
            resource["qualityScore"] = self.publication_quality_score(current["publication_id"])
            resource.setdefault("category", row.get("category") or "")
            resource.setdefault("facets", {})
        elif self.kind == "dataset":
            from app.modules.datasets.publishing import sample_preview

            resource["preview"] = sample_preview(resource.get("sampleData", ""))
            resource["category"] = next(iter(row["data_applications"]), "")
            resource["facets"] = {
                "dataApplication": row["data_applications"],
                "dataShape": row["data_shapes"],
                "aiPurpose": [row["ai_purpose"]],
                "language": resource.get("language", []),
                "dataFormat": resource.get("dataFormat", []),
            }
            resource["qualityScore"] = None
            resource.update(
                dataLineage=row["data_lineage"] or "",
                storageLocation=row["storage_location"] or "",
                apiEndpoint=row["api_endpoint"] or "",
            )
        else:
            resource.setdefault("category", row.get("category") or "")
            resource.setdefault("facets", {})
        return resource

    def publication_quality_score(self, publication_id: UUID) -> float | None:
        """读取当前发布版本的审核质量分，兼容尚未保存派生分值的历史版本。"""
        cached = getattr(self, "_catalog_quality_scores", {})
        if publication_id in cached:
            return cached[publication_id]
        reviews = self.table("resource_reviews")
        scores = self.db.scalar(
            select(reviews.c.quality_scores)
            .where(
                reviews.c.resource_type == self.kind,
                reviews.c.publication_id == publication_id,
                reviews.c.decision == "approved",
                reviews.c.quality_scores.is_not(None),
            )
            .order_by(reviews.c.created_at.desc(), reviews.c.id)
            .limit(1)
        )
        return review_quality_score(scores)

    def resource_metrics(self, resource_id: UUID) -> dict:
        """只统计平台已记录的有效行为，不把审核质量分当作用户评分。"""
        if resource_id in getattr(self, "_catalog_metrics", {}):
            return deepcopy(self._catalog_metrics[resource_id])
        favorites, ratings, downloads = (
            self.table("resource_favorites"),
            self.table("resource_ratings"),
            self.table("resource_downloads"),
        )

        def conditions(table: Table) -> tuple:
            return (table.c.resource_type == self.kind, table.c.resource_id == resource_id)

        average, rating_count = self.db.execute(
            select(func.avg(ratings.c.rating), func.count()).where(*conditions(ratings))
        ).one()
        download_count, last_download = self.db.execute(
            select(func.count(), func.max(downloads.c.completed_at)).where(
                *conditions(downloads), downloads.c.status == "completed"
            )
        ).one()
        logs = self.table("operation_logs")
        usage = self.db.scalar(
            select(func.count())
            .select_from(logs)
            .where(
                logs.c.target_id == str(resource_id),
                logs.c.action == "invoke",
                logs.c.target_type == {"knowledge": "knowledge_base"}.get(self.kind, self.kind),
            )
        )
        return {
            "favoriteCount": self.db.scalar(
                select(func.count()).select_from(favorites).where(*conditions(favorites))
            ),
            "rating": round(float(average), 2) if average is not None else None,
            "ratingCount": rating_count,
            "downloadCount": download_count,
            "lastDownloadedAt": iso(last_download),
            "usage": usage or 0,
            "metricsScope": "platform",
        }

    def put_resource(self, resource: dict) -> None:
        """缓存服务产生的正式投影，发布时与审核一并保存，不存派生统计。"""
        self.pending_resources[resource["id"]] = deepcopy(resource)

    def save_resource_state(self, resource: dict) -> None:
        """仅维护启用状态，正式内容修改必须新建发布申请。"""
        main = self.table(self.main_name)
        resource_id = identifier(resource["id"])
        self.lock("resource:" + resource["id"])
        row = self.db.execute(select(main).where(main.c.id == resource_id)).mappings().first()
        if row is None:
            raise HTTPException(409, "资源必须通过发布申请创建")
        state = resource.get("listingStatus")
        desired = {"listed": "enabled", "unlisted": "disabled"}.get(state, row["status"])
        if desired != row["status"]:
            self.db.execute(
                update(main)
                .where(main.c.id == resource_id)
                .values(status=desired, updated_at=utc_now())
            )

    def publish_version(self, publication: dict, row: dict, review: dict) -> tuple:
        """在审核事务内固化正式资料、不可变版本、依赖和教程引用。

        首次发布依赖主表的默认禁用状态；后续发布保留原有启停状态。主表概览与
        current_version_id 在同一事务更新，读者不会看到新概览搭配旧版本的中间状态。
        申请基线与当前版本不一致时返回 409，要求申请人基于最新资料重新提交。
        """
        main = self.table(self.main_name)
        resource_id = identifier(publication[self.resource_key])
        resource = self.pending_resources.pop(str(resource_id), None) or deepcopy(
            publication["data"]
        )
        current = self.db.execute(select(main).where(main.c.id == resource_id)).mappings().first()
        creator_id = row["applicant_id"]
        values = self.main_values(resource, creator_id=creator_id if current is None else None)
        version_id = identifier(publication.get("versionId") or str(uuid4()))
        published_at = date_value(review.get("reviewedAt")) or utc_now()
        if current is None:
            self.db.execute(insert(main).values(id=resource_id, **values))
        else:
            # 多个申请可以基于同一旧版本送审，但首个发布后，其余申请不能覆盖它的成果。
            if row["base_version_id"] != current["current_version_id"]:
                raise HTTPException(409, "资源版本已更新，请基于最新版本重新提交")
            self.db.execute(update(main).where(main.c.id == resource_id).values(**values))
        versions = self.table(self.prefix + "_versions")
        self.db.execute(
            insert(versions).values(
                id=version_id,
                **{self.prefix + "_id": resource_id},
                publication_id=row["id"],
                version=row["version"],
                version_notes=row["version_notes"],
                snapshot=deepcopy(row["data"]),
                publisher_id=self.foundation.user_id(self.db, review["reviewerId"]),
                published_at=published_at,
            )
        )
        if self.kind == "application":
            dependencies = self.table("application_dependencies")
            for position, dependency in enumerate(row["data"].get("dependencies", []), start=1):
                self.db.execute(
                    insert(dependencies).values(
                        id=uuid4(),
                        version_id=version_id,
                        resource_type=dependency["kind"],
                        resource_id=identifier(dependency["resourceId"]),
                        resource_version_id=identifier(dependency["resourceVersionId"]),
                        position=position,
                    )
                )
        self.archive_tutorials(row["id"], "version", version_id)
        self.db.execute(
            update(main).where(main.c.id == resource_id).values(current_version_id=version_id)
        )
        publication.update(
            versionId=str(version_id),
            listingStatus="unlisted"
            if current is None
            else ("listed" if current["status"] == "enabled" else "unlisted"),
        )
        return resource_id, version_id, published_at
