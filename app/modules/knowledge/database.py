"""知识库页面适配：正式资料和文档审核入库，模拟索引仅保留短期外部运行态。"""

import json
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, update

from app.db.foundation import date_value, identifier, iso, utc_now
from app.modules.common.database_workspace import DatabaseResourceWorkspace
from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase, ResourceDatabaseSession
from app.modules.knowledge.schemas import IndexInput

FORM_FIELDS = (
    "name",
    "description",
    "descriptionMarkdown",
    "category",
    "owner",
    "contact",
    "dataLevel",
)
RUNTIME_FIELDS = (
    "indexConfig",
    "chunks",
    "status",
    "needsRebuild",
    "indexError",
    "indexBuildMs",
    "retrievalCount",
    "retrievalSuccessCount",
    "retrievalElapsedMs",
    "jobId",
    "jobStartedAt",
)


class KnowledgeDatabase:
    """保持现有知识库服务契约，保存时拆分到确定的业务表，不写整库 JSON。"""

    def __init__(self, resources: ResourceDatabase) -> None:
        self.resources = resources

    def public_record(self, record: dict) -> dict | None:
        """公开概览只用当前已发布版本，不能泄露后续申请的待审修改。"""
        with self.resources.transaction(is_readonly=True) as unit:
            resource = unit.get_resource(record["id"])
        if resource is None or resource["listingStatus"] != "listed":
            return None
        result = deepcopy(record)
        result.update({field: resource.get(field, "") for field in FORM_FIELDS})
        result.update(
            review={**record["review"], "status": "approved"},
            listingStatus="listed",
            visibility=resource["visibility"],
        )
        return result

    def set_listing(self, base_id: str, status: str, revision: int, actor: Actor) -> dict:
        """锁定知识库修订后启停正式版本，不能以申请修订覆盖并发编辑。"""
        with self.resources.transaction() as unit:
            unit.lock("knowledge-state:" + base_id)
            record = self._read(unit, base_id)
            if record is None or record["revision"] != revision:
                raise HTTPException(409, "知识库已更新，请刷新")
            resource = DatabaseResourceWorkspace.check_resource(
                unit.get_resource(base_id), actor, True
            )
            resource["listingStatus"] = status
            unit.save_resource_state(resource)
            return self._read(unit, base_id)

    def _publication(self, unit: ResourceDatabaseSession, base_id: str) -> dict | None:
        table = unit.table("knowledge_base_publications")
        row = (
            unit.db.execute(
                select(table)
                .where(
                    or_(
                        table.c.knowledge_base_id == identifier(base_id),
                        table.c.data["publicationContext"]["plannedResourceId"].as_string()
                        == base_id,
                    )
                )
                .order_by(table.c.created_at.desc(), table.c.updated_at.desc(), table.c.id)
                .limit(1)
            )
            .mappings()
            .first()
        )
        return unit.publication_view(dict(row)) if row else None

    def _read(self, unit: ResourceDatabaseSession, base_id: str) -> dict | None:
        publication = self._publication(unit, base_id)
        resource = unit.get_resource(base_id)
        if publication is None and resource is None:
            return None
        data = deepcopy(publication["data"] if publication else resource)
        author = publication or resource
        base = {
            **data,
            "id": base_id,
            "publicationId": publication["id"] if publication else base_id,
            "reviewRevision": publication["revision"] if publication else 1,
            "createdBy": author["createdBy"],
            "createdByName": author["createdByName"],
            "organizationId": author.get("organizationId", ""),
            "createdAt": author["createdAt"],
            "updatedAt": author["updatedAt"],
            "tone": "blue",
            "documents": [],
            "reviewHistory": [],
            "versions": resource["versions"] if resource else [],
            "listingStatus": resource["listingStatus"] if resource else "unlisted",
            "visibility": resource["visibility"]
            if resource
            else {"scope": "members", "targets": [author["createdBy"]], "revision": 1},
            "status": "ready",
            "indexConfig": data.get("indexConfig", IndexInput().model_dump(by_alias=True)),
            "chunks": [],
            "needsRebuild": False,
            "indexError": "",
            "indexBuildMs": None,
            "retrievalCount": 0,
            "retrievalSuccessCount": 0,
            "retrievalElapsedMs": 0.0,
            "jobId": None,
            "review": {
                "status": publication["status"] if publication else "approved",
                "submittedAt": publication["submittedAt"] if publication else None,
                "submittedBy": author["createdBy"],
                "submittedName": author["createdByName"],
                "reviewedAt": None,
                "reviewer": "",
                "reason": "",
            },
        }
        for key in FORM_FIELDS:
            base.setdefault(key, "")
        base["description"] = data.get("description") or data.get("summary", "")
        if publication:
            for review in publication["reviews"]:
                value = {
                    "id": base_id,
                    "kind": "base",
                    "name": data["name"],
                    "owner": base["owner"],
                    "status": review["decision"],
                    "reason": review["reason"],
                    "reviewer": review["reviewerName"],
                    "reviewerId": review["reviewerId"],
                    "reviewedAt": review["reviewedAt"],
                    "submittedAt": review["submittedAt"],
                    "submittedName": author["createdByName"],
                    "scores": review.get("scores"),
                    "reportFiles": review.get("reportFiles", []),
                }
                base["reviewHistory"].append(value)
            if publication["reviews"] and publication["status"] != "pending":
                base["review"].update(
                    {key: value[key] for key in ("reason", "reviewer", "reviewedAt")}
                )
        docs, reviews, files = (
            unit.table("knowledge_documents"),
            unit.table("knowledge_document_reviews"),
            unit.table("files"),
        )
        all_docs = list(
            unit.db.execute(
                select(docs, files.c.size_bytes)
                .join(files, files.c.id == docs.c.file_id)
                .where(docs.c.knowledge_base_id == identifier(base_id))
                .order_by(docs.c.created_at, docs.c.id)
            ).mappings()
        )
        for doc in all_docs:
            history = list(
                unit.db.execute(
                    select(reviews)
                    .where(reviews.c.document_id == doc["id"])
                    .order_by(reviews.c.submission_no)
                ).mappings()
            )
            current = None
            for review in history:
                reviewer = unit.foundation.user(unit.db, review["reviewer_id"])
                value = {
                    "id": str(doc["id"]),
                    "kind": "document",
                    "name": doc["name"],
                    "owner": base["owner"],
                    "status": review["decision"],
                    "reason": review["comment"] or "",
                    "reviewer": reviewer["name"],
                    "reviewerId": reviewer["id"],
                    "reviewedAt": iso(review["created_at"]),
                    "submittedAt": iso(review["submitted_at"]),
                }
                base["reviewHistory"].append(value)
                if review["submission_no"] == doc["submission_no"]:
                    current = value
            if doc["deleted_at"]:
                continue
            uploader = unit.foundation.user(unit.db, doc["uploader_id"])
            base["documents"].append(
                {
                    "id": str(doc["id"]),
                    "fileId": str(doc["file_id"]),
                    "name": doc["name"],
                    "type": doc["name"].rsplit(".", 1)[-1].upper(),
                    "size": f"{doc['size_bytes'] / 1024:.1f} KB",
                    "sizeBytes": doc["size_bytes"],
                    "updatedAt": iso(doc["updated_at"]),
                    "status": "pending"
                    if doc["review_status"] == "approved"
                    else "awaiting_review",
                    "chunkCount": 0,
                    "error": "",
                    "review": {
                        "status": doc["review_status"],
                        "submittedAt": iso(doc["submitted_at"]),
                        "submittedBy": uploader["id"],
                        "submittedName": uploader["name"],
                        "reviewer": current["reviewer"] if current else "",
                        "reason": current["reason"] if current else "",
                        "reviewedAt": current["reviewedAt"] if current else None,
                    },
                }
            )
        # 运行态必须匹配数据库审核与文档版本；SQL 回滚后的缓存不能覆盖真实业务状态。
        stamp = sha256(
            json.dumps(
                {
                    "publication": publication and [publication["id"], publication["revision"]],
                    "resource": resource and [resource["revision"], resource["visibility"]],
                    "documents": [[str(doc["id"]), doc["revision"]] for doc in all_docs],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        state = self.resources.runtime.get("index", base_id) if self.resources.runtime else None
        if state and state.get("databaseStamp") == stamp:
            base.update({key: deepcopy(state[key]) for key in RUNTIME_FIELDS if key in state})
            for doc in base["documents"]:
                if doc["review"]["status"] == "approved":
                    doc.update(state.get("documents", {}).get(doc["id"], {}))
        elif any(doc["review"]["status"] == "approved" for doc in base["documents"]):
            base.update(status="stale", needsRebuild=True)
        base["documentCount"] = len(base["documents"])
        base["storageBytes"] = sum(doc["sizeBytes"] for doc in base["documents"])
        base["chunkCount"] = len(base["chunks"])
        base["databaseStamp"] = stamp
        base["revision"] = int(
            sha256((stamp + str((state or {}).get("revision", 0))).encode()).hexdigest()[:12], 16
        )
        return base

    def get(self, base_id: str) -> dict | None:
        """重建业务视图；外部运行态不参与资源身份和权限判断。"""
        with self.resources.transaction(is_readonly=True) as unit:
            return self._read(unit, base_id)

    def list(self) -> list[dict]:
        """合并正式资源与当前发布申请，平台 UUID 始终稳定。"""
        with self.resources.transaction(is_readonly=True) as unit:
            ids = {item["resourceId"] for item in unit.list_publications()}
            ids.update(item["id"] for item in unit.list_resources())
            return [self._read(unit, key) for key in sorted(ids)]

    def save(self, record: dict, expected: int | None) -> dict:
        """按旧页面修订校验后分表保存，索引结果只写模拟工具链 Redis。"""
        key = record["id"]
        with self.resources.transaction() as unit:
            unit.lock("knowledge-state:" + key)
            previous = self._read(unit, key)
            if (previous["revision"] if previous else None) != expected:
                raise HTTPException(409, "知识库已更新，请刷新后重试")
            publication = self._publication(unit, key)
            data = {field: record.get(field, "") for field in FORM_FIELDS}
            data.update(
                summary=record.get("description", ""),
                indexConfig=record["indexConfig"],
                version=publication["data"].get("version", "1.0") if publication else "1.0",
                tags=record.get("tags", []),
            )
            decision = record["review"]["status"]
            if publication is None:
                publication = {
                    "id": str(uuid4()),
                    "resourceId": key,
                    "createdBy": record["createdBy"],
                    "createdByName": record.get("createdByName", ""),
                    "organizationId": record.get("organizationId", ""),
                    "data": data,
                    "status": "pending",
                    "revision": 1,
                    "reviews": [],
                    "submittedAt": record["review"]["submittedAt"],
                }
                unit.put_publication(publication)
            elif decision != publication["status"]:
                if publication["status"] == "pending" and decision in {
                    "approved",
                    "rejected",
                    "needs_changes",
                }:
                    review = next(
                        item for item in reversed(record["reviewHistory"]) if item["kind"] == "base"
                    )
                    publication["reviews"].append(
                        {
                            "id": str(uuid4()),
                            "reviewerId": review["reviewerId"],
                            "reviewerName": review["reviewer"],
                            "decision": decision,
                            "reason": review["reason"],
                            "reviewedAt": review["reviewedAt"],
                            "scores": review.get("scores"),
                            "reportFiles": review.get("reportFiles", []),
                        }
                    )
                else:
                    publication["data"] = data
                    publication["submittedAt"] = record["review"]["submittedAt"]
                publication.update(status=decision, revision=publication["revision"] + 1)
                unit.put_publication(publication)
            elif previous and any(
                previous.get(field, "") != record.get(field, "") for field in FORM_FIELDS
            ):
                if publication["status"] == "approved":
                    # 已发布资料只生成新申请，当前正式版本继续对读者生效。
                    source = unit.get_resource(key)
                    publication = {
                        "id": str(uuid4()),
                        "resourceId": key,
                        "createdBy": record["createdBy"],
                        "createdByName": record["createdByName"],
                        "baseVersionId": source["versionId"],
                        "data": {**data, "version": f"{len(source['versions']) + 1}.0"},
                        "status": "pending",
                        "revision": 1,
                        "reviews": [],
                        "submittedAt": iso(utc_now()),
                    }
                    unit.put_publication(publication)
                elif publication["status"] in {"draft", "rejected", "needs_changes"}:
                    publication.update(data=data, revision=publication["revision"] + 1)
                    unit.put_publication(publication)
                else:
                    raise HTTPException(409, "已送审资料不能原地修改，请退回后重提")
            resource = unit.get_resource(key)
            if resource:
                desired = record["visibility"]
                existing = resource["visibility"]
                if (existing["scope"], existing["targets"]) != (
                    desired["scope"],
                    desired["targets"],
                ):
                    unit.put_visibility(key, {**desired, "revision": existing["revision"] + 1})
            self._documents(unit, record)
            saved = self._read(unit, key)
            if self.resources.runtime:
                state = {
                    field: deepcopy(record[field]) for field in RUNTIME_FIELDS if field in record
                }
                state.update(
                    databaseStamp=saved["databaseStamp"],
                    revision=str(uuid4()),
                    documents={
                        doc["id"]: {
                            field: doc[field] for field in ("status", "chunkCount", "error")
                        }
                        for doc in record["documents"]
                    },
                )
                self.resources.runtime.put("index", key, state)
            elif any(
                previous and previous.get(field) != record.get(field) for field in RUNTIME_FIELDS
            ):
                raise HTTPException(503, "模拟知识库运行态需要 Redis，请检查缓存服务")
            return self._read(unit, key)

    def _documents(self, unit: ResourceDatabaseSession, record: dict) -> None:
        docs, reviews = unit.table("knowledge_documents"), unit.table("knowledge_document_reviews")
        rows = {
            str(row["id"]): dict(row)
            for row in unit.db.execute(
                select(docs).where(
                    docs.c.knowledge_base_id == identifier(record["id"]),
                    docs.c.deleted_at.is_(None),
                )
            ).mappings()
        }
        incoming = {doc["id"]: doc for doc in record["documents"]}
        for key, row in rows.items():
            if key not in incoming:
                unit.db.execute(
                    update(docs)
                    .where(docs.c.id == row["id"])
                    .values(deleted_at=utc_now(), revision=row["revision"] + 1)
                )
        for key, doc in incoming.items():
            row = rows.get(key)
            if row is None:
                if unit.get_resource(record["id"]) is None:
                    raise HTTPException(409, "知识库审核通过后才能上传文档")
                unit.db.execute(
                    insert(docs).values(
                        id=identifier(key),
                        knowledge_base_id=identifier(record["id"]),
                        file_id=identifier(doc.get("fileId", key)),
                        name=doc["name"],
                        uploader_id=unit.foundation.user_id(unit.db, doc["review"]["submittedBy"]),
                        submitted_at=date_value(doc["review"]["submittedAt"]),
                    )
                )
            elif row["review_status"] != doc["review"]["status"]:
                decision = doc["review"]["status"]
                history = next(
                    item
                    for item in reversed(record["reviewHistory"])
                    if item["kind"] == "document" and item["id"] == key
                )
                unit.db.execute(
                    insert(reviews).values(
                        id=uuid4(),
                        document_id=row["id"],
                        submission_no=row["submission_no"],
                        reviewer_id=unit.foundation.user_id(unit.db, history["reviewerId"]),
                        decision=decision,
                        comment=history["reason"] or None,
                        submitted_at=row["submitted_at"],
                        submission_snapshot={
                            "documentId": str(row["id"]),
                            "fileId": str(row["file_id"]),
                            "name": row["name"],
                        },
                    )
                )
                unit.db.execute(
                    update(docs)
                    .where(docs.c.id == row["id"])
                    .values(review_status=decision, revision=row["revision"] + 1)
                )
