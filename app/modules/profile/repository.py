"""个人中心只读列表投影，不重建资源详情、审核历史和运行态。"""

from sqlalchemy import Select, String, cast, func, literal, select, union_all

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase, iso
from app.modules.profile.schemas import ProfileQuery

PUBLICATION_PATHS = {
    "dataset": "datasets",
    "model": "models",
    "application": "applications",
    "tool": "tools",
}


class ProfilePublicationsRepository:
    """在同一数据库快照中筛选本人申请、统计总数并分页。"""

    def __init__(self, foundation: FoundationDatabase) -> None:
        self.foundation = foundation

    def _publication_rows(self, kind: str) -> Select:
        """投影申请的必要字段，保留既有草稿状态和版本展示规则。"""
        table = self.foundation.table(RESOURCE_TABLES[kind][1] + "_publications")
        return select(
            cast(table.c.id, String).label("id"),
            literal(kind).label("kind"),
            table.c.applicant_id.label("user_id"),
            func.coalesce(table.c.data["name"].as_string(), "").label("name"),
            func.coalesce(table.c.review_status, "draft").label("status"),
            func.coalesce(table.c.data["version"].as_string(), "").label("version"),
            table.c.updated_at,
        )

    def _knowledge_rows(self) -> Select:
        """知识库沿用每个资源的最新申请；只有正式资料时按已通过展示。

        当前知识库页面以资源编号打开，不能把每轮申请重复显示成多个知识库。
        先确定全局最新申请再过滤本人，避免显示本人旧申请冒充最新审批状态。
        """
        publications = self.foundation.table("knowledge_base_publications")
        resources = self.foundation.table("knowledge_bases")
        resource_id = func.coalesce(
            cast(publications.c.knowledge_base_id, String),
            publications.c.data["publicationContext"]["plannedResourceId"].as_string(),
        )
        ranked = select(
            resource_id.label("resource_id"),
            publications.c.applicant_id,
            publications.c.data,
            publications.c.review_status,
            publications.c.updated_at,
            func.row_number()
            .over(
                partition_by=resource_id,
                order_by=(
                    publications.c.created_at.desc(),
                    publications.c.updated_at.desc(),
                    publications.c.id,
                ),
            )
            .label("position"),
        ).subquery()
        latest = select(
            ranked.c.resource_id.label("id"),
            literal("knowledge").label("kind"),
            ranked.c.applicant_id.label("user_id"),
            func.coalesce(ranked.c.data["name"].as_string(), "").label("name"),
            func.coalesce(ranked.c.review_status, "draft").label("status"),
            literal("").label("version"),
            ranked.c.updated_at,
        ).where(ranked.c.position == 1)
        without_publication = select(
            cast(resources.c.id, String).label("id"),
            literal("knowledge").label("kind"),
            resources.c.creator_id.label("user_id"),
            resources.c.name,
            literal("approved").label("status"),
            literal("").label("version"),
            resources.c.updated_at,
        ).where(
            resources.c.current_version_id.is_not(None),
            ~select(publications.c.id).where(resource_id == cast(resources.c.id, String)).exists(),
        )
        return select(union_all(latest, without_publication).subquery())

    def list_for_user(self, subject: str, query: ProfileQuery) -> dict:
        """只读取可信 IAM 身份所属记录，筛选和分页在数据库执行。

        总数查询与分页查询共用只读快照；空页仍返回正确总数。关键词按字面包含
        匹配，百分号和下划线不作为 SQL 通配符。排序增加稳定键，避免同时间翻页漂移。
        """
        sources = [
            self._knowledge_rows() if kind == "knowledge" else self._publication_rows(kind)
            for kind in RESOURCE_TABLES
            if not query.kind or kind == query.kind
        ]
        rows = union_all(*sources).subquery()
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, subject)
            conditions = [rows.c.user_id == user_id]
            if query.status:
                conditions.append(rows.c.status == query.status)
            if query.q:
                conditions.append(rows.c.name.icontains(query.q, autoescape=True))
            total = session.scalar(select(func.count()).select_from(rows).where(*conditions))
            items = []
            if total:
                page = session.execute(
                    select(rows)
                    .where(*conditions)
                    .order_by(rows.c.updated_at.desc(), rows.c.kind, rows.c.id)
                    .offset((query.page - 1) * query.page_size)
                    .limit(query.page_size)
                ).mappings()
                for row in page:
                    kind = row["kind"]
                    href = (
                        f"/knowledge?resourceId={row['id']}"
                        if kind == "knowledge"
                        else f"/{PUBLICATION_PATHS[kind]}/publications/{row['id']}"
                    )
                    items.append(
                        {
                            "id": row["id"],
                            "kind": kind,
                            "name": row["name"],
                            "status": row["status"],
                            "version": row["version"],
                            "updatedAt": iso(row["updated_at"]),
                            "href": href,
                        }
                    )
        return {"items": items, "total": total, "page": query.page, "pageSize": query.page_size}
