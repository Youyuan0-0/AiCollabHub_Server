"""关联资源选择器的轻量查询，不加载文件、文档、审核报告或互动详情。"""

from __future__ import annotations

from sqlalchemy import String, func, select
from sqlalchemy.orm import Session

from app.db.foundation import RESOURCE_TABLES, FoundationDatabase
from app.modules.common.identity import Actor, visible


class ResourceOptionCatalog:
    """在同一只读快照内提供应用发布表单可选择的正式资源。"""

    def __init__(self, foundation: FoundationDatabase) -> None:
        self.foundation = foundation

    def list(self, actor: Actor) -> list[dict]:
        """只返回启用且可见的当前版本，维持各模块原有的选项顺序。"""
        with self.foundation.transaction(is_readonly=True) as session:
            return [
                option
                for kind in ("dataset", "knowledge", "model", "tool")
                for option in self._module(session, kind, actor)
            ]

    def _module(self, session: Session, kind: str, actor: Actor) -> list[dict]:
        """仅连接主表、当前版本及可见范围，权限判断复用公共规则。"""
        main_name, prefix, _ = RESOURCE_TABLES[kind]
        main, versions, policies, targets, users = (
            self.foundation.table(name)
            for name in (
                main_name,
                prefix + "_versions",
                "resource_visibility_policies",
                "resource_visibility_targets",
                "users",
            )
        )
        rows = session.execute(
            select(
                main.c.id,
                main.c.name,
                main.c.provider_name,
                versions.c.version,
                versions.c.snapshot["publicationContext"].label("context"),
                versions.c.snapshot["badge"].label("badge"),
                policies.c.scope,
                policies.c.directory_targets,
                users.c.iam_subject,
            )
            .select_from(main)
            .join(versions, versions.c.id == main.c.current_version_id)
            .outerjoin(
                policies,
                (policies.c.resource_type == kind) & (policies.c.resource_id == main.c.id),
            )
            .outerjoin(targets, targets.c.policy_id == policies.c.id)
            .outerjoin(users, users.c.id == targets.c.user_id)
            .where(main.c.status == "enabled")
            .order_by(main.c.id, users.c.iam_subject)
        ).mappings()
        resources = {}
        for row in rows:
            context = row["context"] or {}
            key = str(row["id"])
            resource = resources.setdefault(
                key,
                {
                    "kind": kind,
                    "resourceId": key,
                    "name": row["name"],
                    "owner": row["provider_name"] or context.get("owner", ""),
                    # 知识库当前选择器不展示版本号，保持既有响应契约。
                    "version": "" if kind == "knowledge" else row["version"],
                    "organizationId": context.get("organizationId", ""),
                    "badge": row["badge"],
                    "visibility": {"scope": row["scope"] or "all", "targets": []},
                },
            )
            policy = resource["visibility"]
            if policy["scope"] in {"organizations", "projects"}:
                policy["targets"] = list(row["directory_targets"] or [])
            elif row["iam_subject"]:
                policy["targets"].append(row["iam_subject"])
        if not resources:
            return []
        if kind == "knowledge":
            # 知识库的管理归属来自最新申请，公开概览仍来自当前正式版本。
            self._knowledge_organizations(session, resources)
        items = [item for item in resources.values() if visible(actor, item, item["visibility"])]
        if kind == "tool" and items:
            logs = self.foundation.table("operation_logs")
            resource_id = logs.c.data["toolId"].as_string()
            counts = dict(
                session.execute(
                    select(resource_id, func.count())
                    .where(
                        logs.c.target_type == "tool",
                        logs.c.action == "invoke",
                        resource_id.in_([item["resourceId"] for item in items]),
                        func.jsonb_typeof(logs.c.data["success"]) == "boolean",
                    )
                    .group_by(resource_id)
                ).all()
            )
            items.sort(
                key=lambda item: (bool(item["badge"]), counts.get(item["resourceId"], 0)),
                reverse=True,
            )
        return [
            {key: item[key] for key in ("kind", "resourceId", "name", "owner", "version")}
            for item in items
        ]

    def _knowledge_organizations(self, session: Session, resources: dict[str, dict]) -> None:
        """批量读取最新申请的单位，维持单位管理员的可见范围语义。"""
        table = self.foundation.table("knowledge_base_publications")
        context = table.c.data["publicationContext"]
        key = func.coalesce(
            table.c.knowledge_base_id.cast(String), context["plannedResourceId"].as_string()
        )
        seen = set()
        for row in session.execute(
            select(
                key.label("resource_id"),
                context["organizationId"].as_string().label("organization"),
            )
            .where(key.in_(resources))
            .order_by(table.c.created_at.desc(), table.c.updated_at.desc(), table.c.id)
        ).mappings():
            if row["resource_id"] not in seen:
                resources[row["resource_id"]]["organizationId"] = row["organization"] or ""
                seen.add(row["resource_id"])
