"""个人收藏和订阅的轻量读写，权限依据与资源详情保持一致。"""

from typing import Literal
from uuid import UUID

from sqlalchemy import Select, and_, func, literal, or_, select, union_all

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase, iso
from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.profile.schemas import ProfileQuery

Relation = Literal["favorites", "subscriptions"]
RESOURCE_PATHS = {
    "dataset": "datasets",
    "model": "models",
    "application": "applications",
    "tool": "tools",
}


class ProfileResourcesRepository:
    """只查询本人的互动关系及可见资源概览，不加载审核、评分和调用历史。"""

    def __init__(self, foundation: FoundationDatabase) -> None:
        self.foundation = foundation

    def _resources(self, kind: str, actor: Actor, user_id: UUID) -> Select:
        """在 SQL 中校验启用状态、正式版本和开放范围，默认范围沿用全部登录用户。"""
        name, prefix, _ = RESOURCE_TABLES[kind]
        main = self.foundation.table(name)
        versions = self.foundation.table(prefix + "_versions")
        policies = self.foundation.table("resource_visibility_policies")
        targets = self.foundation.table("resource_visibility_targets")
        query = (
            select(
                main.c.id,
                literal(kind).label("kind"),
                main.c.name,
                versions.c.version,
                main.c.summary.label("description"),
                func.coalesce(
                    func.nullif(main.c.provider_name, ""),
                    versions.c.snapshot["publicationContext"]["owner"].as_string(),
                    "",
                ).label("owner"),
                main.c.updated_at,
            )
            .select_from(
                main.join(
                    versions,
                    and_(
                        versions.c.id == main.c.current_version_id,
                        versions.c[prefix + "_id"] == main.c.id,
                    ),
                ).outerjoin(
                    policies,
                    and_(
                        policies.c.resource_type == kind,
                        policies.c.resource_id == main.c.id,
                    ),
                )
            )
            .where(main.c.status == "enabled")
        )
        if actor.role == "admin":
            return query
        scopes = [
            policies.c.id.is_(None),
            policies.c.scope == "all",
            and_(
                policies.c.scope == "members",
                select(targets.c.id)
                .where(
                    targets.c.policy_id == policies.c.id,
                    targets.c.user_id == user_id,
                )
                .exists(),
            ),
        ]
        if actor.organization_id:
            scopes.append(
                and_(
                    policies.c.scope == "organizations",
                    policies.c.directory_targets.contains([actor.organization_id]),
                )
            )
            if actor.role == "org_admin":
                scopes.append(
                    versions.c.snapshot["publicationContext"]["organizationId"].as_string()
                    == actor.organization_id
                )
        if actor.project_ids:
            scopes.append(
                and_(
                    policies.c.scope == "projects",
                    policies.c.directory_targets.overlap(list(actor.project_ids)),
                )
            )
        return query.where(or_(*scopes))

    def list_for_user(self, actor: Actor, relation: Relation, query: ProfileQuery) -> dict:
        """在同一快照内筛选、计数和分页；订阅沿用现有场景应用功能范围。"""
        kinds = list(RESOURCE_PATHS) if relation == "favorites" else ["application"]
        kinds = [kind for kind in kinds if not query.kind or query.kind == kind]
        result = {"items": [], "total": 0, "page": query.page, "pageSize": query.page_size}
        if not kinds:
            return result
        table = self.foundation.table(
            "resource_favorites" if relation == "favorites" else "resource_follows"
        )
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, actor.id)
            sources = []
            for kind in kinds:
                resources = self._resources(kind, actor, user_id).subquery()
                sources.append(
                    select(resources, table.c.created_at.label("related_at"))
                    .join(
                        table,
                        and_(table.c.resource_type == kind, table.c.resource_id == resources.c.id),
                    )
                    .where(table.c.user_id == user_id)
                )
            rows = union_all(*sources).subquery()
            filters = []
            if query.q:
                matches = rows.c.name.icontains(query.q, autoescape=True)
                if relation == "subscriptions":
                    matches = or_(matches, rows.c.description.icontains(query.q, autoescape=True))
                filters.append(matches)
            result["total"] = session.scalar(select(func.count()).select_from(rows).where(*filters))
            if result["total"]:
                page = session.execute(
                    select(rows)
                    .where(*filters)
                    .order_by(
                        rows.c.related_at.desc(),
                        rows.c.kind,
                        rows.c.id,
                    )
                    .offset((query.page - 1) * query.page_size)
                    .limit(query.page_size)
                ).mappings()
                result["items"] = [
                    {
                        "id": str(row["id"]),
                        "kind": row["kind"],
                        "name": row["name"],
                        "version": row["version"],
                        "updatedAt": iso(row["updated_at"]),
                        "href": f"/{RESOURCE_PATHS[row['kind']]}?resourceId={row['id']}",
                        **(
                            {"description": row["description"], "owner": row["owner"]}
                            if relation == "subscriptions"
                            else {}
                        ),
                    }
                    for row in page
                ]
        return result

    def visible_application_ids(self, actor: Actor, ids: set[str]) -> set[str]:
        """仅核对通知引用的应用，不读取全平台目录；过时或无效编号不可见。"""
        identifiers = []
        for value in ids:
            try:
                identifiers.append(UUID(value))
            except ValueError:
                continue
        if not identifiers:
            return set()
        with self.foundation.transaction(is_readonly=True) as session:
            user_id = self.foundation.user_id(session, actor.id)
            rows = self._resources("application", actor, user_id).subquery()
            return {
                str(value)
                for value in session.scalars(select(rows.c.id).where(rows.c.id.in_(identifiers)))
            }

    def remove_relation(
        self, actor: Actor, relation: Relation, kind: str, resource_id: str
    ) -> None:
        """复用共用互动事务，仅取消本人指定关系，不覆盖评分或另一种关系。

        即使资源已下架或撤回可见范围，用户仍可清理自己的关系；不授予资源访问权。
        """
        repository = ResourceDatabase(self.foundation, kind)
        with repository.transaction() as session:
            session.save_interaction(
                {
                    "actorId": actor.id,
                    session.resource_key: resource_id,
                    "favorite" if relation == "favorites" else "subscribed": False,
                }
            )
