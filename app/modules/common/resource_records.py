"""发布申请、逐轮审核和不可变版本的数据库事务实现。"""

from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from app.db.foundation import RESOURCE_TABLES, date_value, identifier, iso, utc_now

CONTEXT_FIELDS = (
    "organizationId",
    "source",
    "securityLevel",
    "priority",
    "submissionRules",
    "formRevision",
    "owner",
    "contact",
    "baseVersion",
)


class ResourceRecords:
    """把模块的发布契约映射到独立申请表和共用审核表。"""

    def list_publications(self, *, warnings: list[str] | None = None) -> list[dict]:
        """读取真实申请，缺失资源资料不会被演示目录补齐。"""
        table = self.table(self.prefix + "_publications")
        rows = self.db.execute(
            select(table).order_by(table.c.updated_at.desc(), table.c.id)
        ).mappings()
        return [self.publication_view(dict(row)) for row in rows]

    def get_publication(self, publication_id: str) -> dict | None:
        """按稳定申请编号读取资料；写流程读取前锁定本申请。"""
        self.lock("publication:" + publication_id)
        table = self.table(self.prefix + "_publications")
        row = (
            self.db.execute(select(table).where(table.c.id == identifier(publication_id)))
            .mappings()
            .first()
        )
        return self.publication_view(dict(row)) if row else None

    def publication_view(self, row: dict) -> dict:
        """将数据库状态和审核历史转换为既有页面可使用的申请资料。"""
        data = deepcopy(row["data"])
        context = data.pop("publicationContext", {})
        author = self.foundation.user(self.db, row["applicant_id"])
        resource_id = row[self.prefix + "_id"] or context.get("plannedResourceId")
        table = self.table("resource_reviews")
        reviews = []
        for entry in self.db.execute(
            select(table)
            .where(table.c.resource_type == self.kind, table.c.publication_id == row["id"])
            .order_by(table.c.submission_no)
        ).mappings():
            actor = self.foundation.user(self.db, entry["reviewer_id"])
            submitted = deepcopy(entry["submission_snapshot"]["data"])
            submitted.pop("publicationContext", None)
            reports = self.owner_files("review", entry["id"], "review_report")
            reviews.append(
                {
                    "id": str(entry["id"]),
                    "decision": entry["decision"],
                    "reason": entry["comment"] or "",
                    "comment": entry["comment"] or "",
                    "reviewerId": actor["id"],
                    "reviewerName": actor["name"],
                    "reviewer": actor["name"],
                    "createdAt": iso(entry["created_at"]),
                    "scores": deepcopy(entry["quality_scores"]),
                    "data": submitted,
                    "snapshot": submitted,
                    "reportFiles": reports,
                    "reviewedAt": iso(entry["created_at"]),
                    "submittedAt": iso(entry["submitted_at"]),
                }
            )
        result = {
            **{key: context[key] for key in CONTEXT_FIELDS if key in context},
            "id": str(row["id"]),
            self.resource_key: str(resource_id),
            "versionId": str(row["published_version_id"] or context.get("plannedVersionId")),
            "baseVersionId": str(row["base_version_id"]) if row["base_version_id"] else None,
            "createdBy": author["id"],
            "createdByName": author["name"],
            "data": data,
            "versionNotes": row["version_notes"] or "",
            "status": row["review_status"] or "draft",
            "revision": row["revision"],
            "submissionNo": row["submission_no"],
            "reviews": reviews,
            "createdAt": iso(row["created_at"]),
            "updatedAt": iso(row["updated_at"]),
            "submittedAt": iso(row["submitted_at"]),
            "publishedAt": iso(row["published_at"]),
            "reviewComment": reviews[-1]["reason"] if reviews else "",
        }
        result.setdefault("organizationId", "")
        result.setdefault("securityLevel", "internal")
        result.setdefault("priority", "normal")
        result.setdefault("source", {"kind": "manual"})
        # 早期申请把单位和联系人保存在资料内，新申请放在发布上下文中。
        # 读取时兼容两种格式；未记录的信息留空，不改写已冻结的申请。
        for field in ("owner", "contact"):
            result.setdefault(field, data.get(field) or "")
        if self.kind == "dataset":
            published = self.get_resource(str(resource_id)) if row["published_version_id"] else None
            result.update(
                publishedResource=published,
                syncStatus="local_published" if published else "not_ready",
                publicationProvider="local",
                listingStatus=published["listingStatus"] if published else "unlisted",
            )
        return result

    def publication_data(self, record: dict) -> dict:
        """生成可持久化的申请资料，并固定资源、版本及应用依赖编号。

        publicationContext 承接跨模块的发布上下文，plannedResourceId 允许尚无
        主表记录的草稿提前持有稳定资源编号。应用依赖须解析到具体正式版本，
        不能只保存资源编号后在执行或展示历史时随当前版本漂移。
        """
        data = deepcopy(record["data"])
        context = {key: deepcopy(record[key]) for key in CONTEXT_FIELDS if key in record}
        context.update(
            plannedResourceId=record[self.resource_key],
            plannedVersionId=record.setdefault("versionId", str(uuid4())),
        )
        data["publicationContext"] = context
        if self.kind == "application":
            for dependency in data.get("dependencies", []):
                main_name, prefix, _ = RESOURCE_TABLES[dependency["kind"]]
                main = self.table(main_name)
                version_id = self.db.scalar(
                    select(main.c.current_version_id).where(
                        main.c.id == identifier(dependency["resourceId"]),
                        main.c.status == "enabled",
                    )
                )
                if version_id is None:
                    raise HTTPException(422, "关联资源没有可用的正式版本")
                if dependency.get("resourceVersionId"):
                    versions = self.table(prefix + "_versions")
                    version_id = self.db.scalar(
                        select(versions.c.id).where(
                            versions.c.id == identifier(dependency["resourceVersionId"]),
                            versions.c[prefix + "_id"] == identifier(dependency["resourceId"]),
                        )
                    )
                    if version_id is None:
                        raise HTTPException(422, "关联版本不属于指定资源")
                dependency["resourceVersionId"] = str(version_id)
            record["data"]["dependencies"] = deepcopy(data.get("dependencies", []))
        return data

    def put_publication(self, record: dict) -> None:
        """在调用方事务内保存申请或完成审核，不自行提交。

        record.revision 是期望写入的新修订，须为数据库当前值加一。先锁申请再
        比对修订，避免多个 worker 先读后写导致覆盖。草稿在库中用空 review_status
        表示；送审另行增加 submission_no，并再次推进 revision，返回值以回填为准。
        """
        publication_id = identifier(record["id"])
        self.lock("publication:" + record["id"])
        table = self.table(self.prefix + "_publications")
        row = self.db.execute(select(table).where(table.c.id == publication_id)).mappings().first()
        current = dict(row) if row else None
        if current is not None and record["revision"] != current["revision"] + 1:
            raise HTTPException(409, "申请已更新，请刷新后重试")
        target_status = None if record["status"] == "draft" else record["status"]
        if (
            current is not None
            and current["review_status"] == "pending"
            and target_status in {"approved", "rejected", "needs_changes"}
        ):
            self.conclude_publication(record, current, target_status)
            return
        if current is not None and current["review_status"] in {"pending", "approved"}:
            raise HTTPException(409, "待审或已通过资料不能直接修改，请创建新的申请")
        data = self.publication_data(record)
        now = utc_now()
        values = {
            "data": data,
            "version": data.get("version") or None,
            "version_notes": (
                record.get("versionNotes") if self.kind == "tool" else data.get("versionNotes")
            )
            or None,
            "updated_at": now,
            "base_version_id": identifier(record["baseVersionId"])
            if record.get("baseVersionId")
            else None,
        }
        resource_id = record[self.resource_key]
        main = self.table(self.main_name)
        self.lock("resource:" + resource_id)
        existing = (
            self.db.execute(select(main).where(main.c.id == identifier(resource_id)))
            .mappings()
            .first()
        )
        if existing is not None and values["base_version_id"] is None:
            values["base_version_id"] = existing["current_version_id"]
        if current is None:
            self.db.execute(
                insert(table).values(
                    id=publication_id,
                    applicant_id=self.foundation.user_id(self.db, record["createdBy"]),
                    **{self.prefix + "_id": existing["id"] if existing else None},
                    revision=1,
                    created_at=now,
                    **values,
                )
            )
            current = dict(
                self.db.execute(select(table).where(table.c.id == publication_id)).mappings().one()
            )
        else:
            self.db.execute(
                update(table)
                .where(table.c.id == publication_id)
                .values(
                    **values,
                    revision=current["revision"] + 1,
                )
            )
            current = {**current, **values, "revision": current["revision"] + 1}
        self.sync_tutorials(publication_id, data, current["applicant_id"])
        if target_status == "pending":
            # 先保存资料和教程引用，再切换待审状态；进入待审后数据库规则会冻结本轮内容。
            if not values["version"]:
                raise HTTPException(422, "提交审核前请填写版本号")
            current.update(
                review_status="pending",
                submission_no=current["submission_no"] + 1,
                submitted_at=date_value(record.get("submittedAt")) or now,
                revision=current["revision"] + 1,
            )
            self.db.execute(
                update(table)
                .where(table.c.id == publication_id)
                .values(
                    review_status=current["review_status"],
                    submission_no=current["submission_no"],
                    submitted_at=current["submitted_at"],
                    revision=current["revision"],
                )
            )
        record.update(
            revision=current["revision"],
            status=current["review_status"] or "draft",
            submissionNo=current["submission_no"],
            submittedAt=iso(current["submitted_at"]),
            updatedAt=iso(now),
            baseVersionId=str(current["base_version_id"]) if current["base_version_id"] else None,
        )

    def conclude_publication(self, record: dict, row: dict, decision: str) -> None:
        """归档本轮审核后更新结论，通过时再生成正式版本和发布结果。

        审核快照取自已锁定的数据库 row，而非客户端传来的可变资料。文件历史、
        申请结论、主表与版本共用当前事务，任何一步失败都不能留下部分通过的状态。
        """
        if not record.get("reviews"):
            raise HTTPException(422, "缺少审核记录")
        review = record["reviews"][-1]
        review_id = identifier(review["id"])
        table = self.table(self.prefix + "_publications")
        reviews = self.table("resource_reviews")
        self.db.execute(
            insert(reviews).values(
                id=review_id,
                resource_type=self.kind,
                publication_id=row["id"],
                submission_no=row["submission_no"],
                reviewer_id=self.foundation.user_id(self.db, review["reviewerId"]),
                decision=decision,
                comment=review.get("reason") or review.get("comment") or None,
                quality_scores=review.get("scores"),
                submitted_at=row["submitted_at"],
                submission_snapshot={
                    "version": row["version"],
                    "versionNotes": row["version_notes"],
                    "data": row["data"],
                },
                created_at=date_value(review.get("reviewedAt")) or utc_now(),
            )
        )
        self.archive_tutorials(row["id"], "review", review_id)
        self.archive_reports(row, review_id, review)
        revision = row["revision"] + 1
        self.db.execute(
            update(table)
            .where(table.c.id == row["id"])
            .values(
                review_status=decision,
                revision=revision,
                updated_at=utc_now(),
            )
        )
        record["status"] = decision
        review["decision"] = decision
        if decision == "approved":
            resource_id, version_id, published_at = self.publish_version(record, row, review)
            revision += 1
            self.db.execute(
                update(table)
                .where(table.c.id == row["id"])
                .values(
                    **{self.prefix + "_id": resource_id},
                    published_version_id=version_id,
                    published_at=published_at,
                    revision=revision,
                    updated_at=utc_now(),
                )
            )
            record["publishedAt"] = iso(published_at)
        record["revision"] = revision
