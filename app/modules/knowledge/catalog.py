"""知识库目录的批量读取，所有关联资料均来自同一个只读事务。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select

from app.db.foundation import identifier
from app.modules.common.resource_database import ResourceDatabaseSession


@dataclass
class KnowledgeCatalogSnapshot:
    """按知识库和文档编号分组的请求内资料，不作为跨请求缓存。"""

    publications: dict[str, dict]
    resources: dict[str, dict]
    documents: dict[str, list[dict]]
    document_reviews: dict[UUID, list[dict]]

    @classmethod
    def load(
        cls, unit: ResourceDatabaseSession, base_id: str | None = None
    ) -> "KnowledgeCatalogSnapshot":
        """批量选择最新申请、正式版本和文档，保留删除记录参与修订指纹。"""
        table = unit.table("knowledge_base_publications")
        query = select(table).order_by(
            table.c.created_at.desc(), table.c.updated_at.desc(), table.c.id
        )
        if base_id is not None:
            query = query.where(
                or_(
                    table.c.knowledge_base_id == identifier(base_id),
                    table.c.data["publicationContext"]["plannedResourceId"].as_string() == base_id,
                )
            ).limit(1)
        latest = {}
        for row in unit.db.execute(query).mappings():
            key = row["knowledge_base_id"] or row["data"].get("publicationContext", {}).get(
                "plannedResourceId"
            )
            latest.setdefault(str(key), dict(row))
        unit.prefetch_publications(list(latest.values()))
        publications = {key: unit.publication_view(row) for key, row in latest.items()}
        resources = {
            item["id"]: item
            for item in unit.list_resources([base_id] if base_id is not None else None)
        }
        documents = {key: [] for key in publications.keys() | resources.keys()}
        result = cls(publications, resources, documents, {})
        if not documents:
            return result
        docs, files, reviews = (
            unit.table("knowledge_documents"),
            unit.table("files"),
            unit.table("knowledge_document_reviews"),
        )
        user_ids = set()
        for row in unit.db.execute(
            select(docs, files.c.size_bytes)
            .join(files, files.c.id == docs.c.file_id)
            .where(docs.c.knowledge_base_id.in_([identifier(key) for key in documents]))
            .order_by(docs.c.created_at, docs.c.id)
        ).mappings():
            documents[str(row["knowledge_base_id"])].append(dict(row))
            result.document_reviews[row["id"]] = []
            if row["deleted_at"] is None:
                user_ids.add(row["uploader_id"])
        if result.document_reviews:
            for row in unit.db.execute(
                select(reviews)
                .where(reviews.c.document_id.in_(result.document_reviews))
                .order_by(reviews.c.submission_no)
            ).mappings():
                result.document_reviews[row["document_id"]].append(dict(row))
                user_ids.add(row["reviewer_id"])
        unit.foundation.prefetch_users(unit.db, user_ids)
        return result
