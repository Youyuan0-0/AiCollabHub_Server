"""工具业务流程；通过统一仓储事务保存发布、审核和管理操作。"""

import json
from copy import deepcopy
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException, Request

from app.modules.common.catalog import query_resources
from app.modules.common.content_validation import sensitive_errors
from app.modules.common.fixtures import paginate
from app.modules.common.identity import Actor, can_manage, require_admin, visible
from app.modules.common.memory_repository import InMemoryResourceRepository
from app.modules.common.review_report_rules import public_report, validate_reports
from app.modules.common.schema_validation import validate_schema_input
from app.modules.common.schemas import CatalogQuery
from app.modules.common.service import timestamp
from app.modules.common.transaction_ports import ResourceRepository, ResourceSession
from app.modules.tools.fields import OPTIONAL_FIELDS, REQUIRED_FIELDS
from app.modules.tools.providers import (
    ToolProvider,
    create_tool_provider,
    default_configuration,
    seed_tools,
)
from app.modules.tools.schemas import (
    InvokeTool,
    ReviewTool,
    SaveTool,
    ToolAuditQuery,
    ToolInput,
    ToolListing,
    ToolPriority,
    ToolVisibility,
)


def public_tool(resource: dict) -> dict:
    """普通目录移除后台管理字段，运行地址和内部范围不下发到浏览器。"""
    private = {
        "endpoint",
        "dataLevel",
        "contact",
        "organizationId",
        "createdBy",
        "securityLevel",
        "source",
    }
    result = {key: value for key, value in resource.items() if key not in private}
    result.pop("metadata", None)
    return result


def ensure_revision(record: dict, revision: int | None) -> None:
    """阻止旧页面覆盖已保存或已审核的资料。"""
    if record["revision"] != revision:
        raise HTTPException(409, "资料已更新，请刷新后重试；当前修改未覆盖服务端内容")


class ToolWorkspace:
    """提供工具登记、审核、展示、互动、模拟执行和调用记录。"""

    def __init__(
        self, repository: ResourceRepository | None = None, provider: ToolProvider | None = None
    ) -> None:
        self.repository = repository or InMemoryResourceRepository(
            resources=seed_tools(), configuration=default_configuration()
        )
        self.provider = provider or create_tool_provider()

    def configuration(self) -> dict:
        """读取共享的工具表单与筛选字典。"""
        with self.repository.transaction(is_readonly=True) as session:
            return session.get_configuration()

    def precheck(self, data: ToolInput) -> dict:
        """复用发布拦截词检查；前端预检和最终提交使用同一份规则。"""
        errors = sensitive_errors(
            data.model_dump(by_alias=True), self.configuration()["sensitiveTerms"]
        )
        return {"fieldErrors": errors}

    def _resource(
        self, session: ResourceSession, tool_id: str, actor: Actor, admin: bool = False
    ) -> dict:
        resource = session.get_resource(tool_id)
        if resource is None:
            raise HTTPException(404, "工具不存在")
        if admin:
            require_admin(actor)
            if not can_manage(actor, resource):
                raise HTTPException(403, "无权管理其他单位的工具")
        elif resource["listingStatus"] != "listed" or not visible(
            actor, resource, session.get_visibility(tool_id)
        ):
            raise HTTPException(404, "工具不存在或当前不可访问")
        return resource

    def _summary(self, session: ResourceSession, resource: dict, actor: Actor) -> dict:
        interactions = [
            item for item in session.entries("interactions") if item["toolId"] == resource["id"]
        ]
        ratings = [item["rating"] for item in interactions if item.get("rating")]
        mine = next((item for item in interactions if item["actorId"] == actor.id), {})
        runs = [item for item in session.entries("runs") if item["toolId"] == resource["id"]]
        successful = sum(item["success"] for item in runs)
        return {
            **resource,
            "runtimeMode": self.provider.mode,
            "usage": len(runs),
            "favoriteCount": sum(bool(item.get("favorite")) for item in interactions),
            "rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
            "ratingCount": len(ratings),
            "isFavorite": mine.get("favorite", False),
            "myRating": mine.get("rating", 0),
            "myComment": mine.get("comment", ""),
            "runtime": {
                "total": len(runs),
                "today": sum(item["createdAt"][:10] == timestamp()[:10] for item in runs),
                "averageResponseMs": round(sum(item["durationMs"] for item in runs) / len(runs), 2)
                if runs
                else None,
                "successRate": round(successful / len(runs) * 100, 2) if runs else None,
                "errorRate": round((len(runs) - successful) / len(runs) * 100, 2) if runs else None,
            },
        }

    def catalog(
        self,
        query: CatalogQuery,
        actor: Actor,
        admin: bool = False,
        *,
        favorites_only: bool = False,
    ) -> dict:
        """统一分页、搜索和组合筛选；权限过滤早于统计与筛选项生成。"""
        if admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            records = [
                self._summary(session, item, actor)
                for item in session.list_resources()
                if (
                    can_manage(actor, item)
                    if admin
                    else (
                        item["listingStatus"] == "listed"
                        and visible(actor, item, session.get_visibility(item["id"]))
                    )
                )
            ]
            favorites = sum(item["isFavorite"] for item in records)
            page = query_resources(
                [item for item in records if item["isFavorite"]] if favorites_only else records,
                query,
            )
            return {
                **page,
                "items": page["items"] if admin else [public_tool(item) for item in page["items"]],
                "filterGroups": [
                    {
                        "id": key,
                        "label": label,
                        "options": sorted(
                            {v for item in records for v in item["facets"].get(key, []) if v}
                        ),
                    }
                    for key, label in (
                        ("toolType", "工具类型"),
                        ("tags", "功能标签"),
                        ("sourceType", "来源类型"),
                        ("team", "研发团队"),
                    )
                ],
                "mode": self.provider.mode,
                "favoriteCount": favorites,
            }

    def resource(self, tool_id: str, actor: Actor, admin: bool = False) -> dict:
        """读取当前工具详情及账号互动状态，后台增加可见范围。"""
        with self.repository.transaction(is_readonly=True) as session:
            resource = self._summary(session, self._resource(session, tool_id, actor, admin), actor)
            if admin:
                return {**resource, "visibility": session.get_visibility(tool_id)}
            return public_tool(resource)

    def _publication(
        self, session: ResourceSession, record_id: str, actor: Actor, admin: bool
    ) -> dict:
        record = session.get_publication(record_id)
        if not record:
            raise HTTPException(404, "发布申请不存在")
        if admin:
            require_admin(actor)
            allowed = can_manage(actor, record)
        else:
            allowed = record["createdBy"] == actor.id
        if not allowed:
            raise HTTPException(403, "无权查看或修改该发布申请")
        return record

    def publications(self, actor: Actor, admin: bool = False) -> list[dict]:
        """按用户或管理员范围返回申请，并附加资源当前上下架状态。"""
        if admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            records = [
                item
                for item in session.list_publications()
                if (can_manage(actor, item) if admin else item["createdBy"] == actor.id)
            ]
            for item in records:
                resource = session.get_resource(item["toolId"])
                item["listingStatus"] = resource["listingStatus"] if resource else None
            return sorted(records, key=lambda item: item["updatedAt"], reverse=True)

    def publication(self, record_id: str, actor: Actor, admin: bool = False) -> dict:
        """读取申请完整资料；已提交或已批准申请供只读展示。"""
        with self.repository.transaction(is_readonly=True) as session:
            record = self._publication(session, record_id, actor, admin)
            resource = session.get_resource(record["toolId"])
            return {**record, "versions": resource.get("versions", []) if resource else []}

    def new_version(self, tool_id: str, actor: Actor) -> dict:
        """同一工具生成唯一待发布版本，旧版本保持可用直到新审核通过。"""
        with self.repository.transaction() as session:
            resource = session.get_resource(tool_id)
            if not resource or resource["createdBy"] != actor.id:
                raise HTTPException(403, "只能更新本人发布的工具")
            existing = next(
                (
                    item
                    for item in session.list_publications()
                    if item["toolId"] == tool_id and item["status"] != "approved"
                ),
                None,
            )
            if existing:
                return existing
            fields = {field.alias or key for key, field in ToolInput.model_fields.items()}
            data = ToolInput.model_validate(
                {key: value for key, value in resource.items() if key in fields}
            )
            used = {item["version"] for item in resource["versions"]}
            number = len(used) + 1
            while f"v{number}.0" in used:
                number += 1
            data.version = f"v{number}.0"
            record = self._prepare_publication(session, SaveTool(data=data), actor, resource)
            record["data"]["version"] = data.version
            record.update(
                toolId=tool_id,
                baseVersion=resource["version"],
                source=resource.get("source", {}),
            )
            session.put_publication(record)
            return record

    def save(
        self, payload: SaveTool, actor: Actor, defaults: dict, record_id: str | None = None
    ) -> dict:
        """保存本人草稿；手工登记与导入复用同一事务内的保存规则。"""
        with self.repository.transaction() as session:
            record = self._prepare_publication(session, payload, actor, defaults, record_id)
            session.put_publication(record)
            return record

    def _prepare_publication(
        self,
        session: ResourceSession,
        payload: SaveTool,
        actor: Actor,
        defaults: dict,
        record_id: str | None = None,
    ) -> dict:
        """校验并组装申请，调用方补齐版本及来源后统一保存一次。"""
        if actor.role not in ("publisher", "admin", "org_admin"):
            raise HTTPException(403, "当前账号没有发布权限")
        previous = self._publication(session, record_id, actor, False) if record_id else None
        if previous:
            ensure_revision(previous, payload.revision)
            if previous["status"] not in ("draft", "rejected", "needs_changes"):
                raise HTTPException(409, "已提交的申请不能修改")
        data = payload.data.model_dump(by_alias=True)
        # 普通发布的版本由服务端维护；工具链导入在内部保留外部版本。
        data["version"] = previous["data"]["version"] if previous else "v1.0"
        if payload.submit:
            config = session.get_configuration()
            for field, key in (
                ("tool_type", "toolTypes"),
                ("source_type", "sourceTypes"),
                ("deployment", "deployments"),
            ):
                if getattr(payload.data, field) not in config[key]:
                    raise HTTPException(422, "请选择当前有效的工具分类、来源和部署方式")
            if payload.data.data_level not in ("核心数据", "重要数据", "一般数据"):
                raise HTTPException(422, "请选择有效的数据分级")
            for field, schema, label in (
                ("inputExample", "inputSchema", "输入示例"),
                ("outputExample", "outputSchema", "输出示例"),
            ):
                if data[field]:
                    try:
                        example = json.loads(data[field])
                    except (ValueError, RecursionError) as cause:
                        raise HTTPException(422, f"{label}必须是有效 JSON") from cause
                    validate_schema_input(data[schema], example, label)
            errors = sensitive_errors(data, config["sensitiveTerms"])
            if errors:
                labels = {**REQUIRED_FIELDS, **OPTIONAL_FIELDS}
                raise HTTPException(
                    422, "资料命中发布拦截词：" + "、".join(labels.get(key, key) for key in errors)
                )
        now = timestamp()
        record = {
            **(previous or {}),
            "id": record_id or str(uuid4()),
            "toolId": previous["toolId"] if previous else str(uuid4()),
            "createdBy": actor.id,
            "organizationId": actor.organization_id,
            "owner": defaults["owner"],
            "contact": defaults["contact"],
            "createdAt": previous["createdAt"] if previous else now,
            "updatedAt": now,
            "data": data,
            "submittedAt": now if payload.submit else (previous or {}).get("submittedAt"),
            "priority": (previous or {}).get("priority", "normal"),
            "source": (previous or {}).get(
                "source",
                {"kind": "manual", "label": "平台登记", "externalId": "", "syncedAt": None},
            ),
            "status": "pending" if payload.submit else "draft",
            "revision": previous["revision"] + 1 if previous else 1,
            "reviews": previous["reviews"] if previous else [],
        }
        return record

    def review(self, record_id: str, payload: ReviewTool, actor: Actor) -> dict:
        """审核快照与目录发布同一事务完成；退回允许发布者修改重提。"""
        with self.repository.transaction() as session:
            record = self._publication(session, record_id, actor, True)
            ensure_revision(record, payload.revision)
            if record["status"] != "pending":
                raise HTTPException(409, "只有待审核申请可以审核")
            reports = validate_reports(session, record, payload.report_file_ids, actor)
            event = {
                "id": uuid4().hex,
                "reviewerId": actor.id,
                "reviewerName": actor.name,
                "publicationId": record_id,
                "toolId": record["toolId"],
                "name": record["data"]["name"],
                "organizationId": record["organizationId"],
                "reviewer": actor.name,
                "createdAt": timestamp(),
                **payload.model_dump(by_alias=True),
                "snapshot": deepcopy(record["data"]),
                "reportFiles": [public_report(file) for file in reports],
            }
            record.update(
                status=payload.decision, revision=record["revision"] + 1, updatedAt=timestamp()
            )
            record["reviews"].append(event)
            if payload.decision == "approved":
                data = record["data"]
                previous = session.get_resource(record["toolId"])
                if previous and previous["version"] != record.get("baseVersion"):
                    raise HTTPException(409, "已发布版本发生变化，请重新读取资料")
                changes = [key for key in data if not previous or previous.get(key) != data[key]]
                labels = {
                    **REQUIRED_FIELDS,
                    **OPTIONAL_FIELDS,
                    "version": "版本号",
                    "inputExample": "输入示例",
                    "outputExample": "输出示例",
                }
                resource = {
                    **data,
                    "id": record["toolId"],
                    "kind": "tool",
                    "tone": "blue",
                    "owner": record["owner"],
                    "contact": record["contact"],
                    "createdBy": record["createdBy"],
                    "organizationId": record["organizationId"],
                    "category": data["toolType"],
                    "status": "available",
                    "listingStatus": previous["listingStatus"] if previous else "listed",
                    "revision": previous["revision"] + 1 if previous else 1,
                    "createdAt": previous["createdAt"] if previous else record["createdAt"],
                    "updatedAt": timestamp(),
                    "source": record["source"],
                    "usage": 0,
                    "rating": None,
                    "facets": {
                        "toolType": [data["toolType"]],
                        "sourceType": [data["sourceType"]],
                        "team": [data["team"]],
                        "tags": data["tags"],
                    },
                    "versions": [
                        {
                            "version": data["version"],
                            "releasedAt": timestamp(),
                            "status": "current",
                            "note": "首次发布"
                            if not previous
                            else "更新：" + "、".join(labels.get(key, key) for key in changes),
                            "changedFields": changes,
                        }
                    ]
                    + [
                        {**item, "status": "previous"}
                        for item in (previous or {}).get("versions", [])
                    ],
                }
                session.put_resource(resource)
            session.put_publication(record)
            for file in reports:
                session.put_entry("review_files", file["id"], {**file, "reviewId": event["id"]})
            session.put_entry("audit", event["id"], event)
            return record

    def prioritize(self, record_id: str, payload: ToolPriority, actor: Actor) -> dict:
        """管理员设置队列优先级并留痕；不改变资料修订以免已暂存报告失效。"""
        with self.repository.transaction() as session:
            record = self._publication(session, record_id, actor, True)
            ensure_revision(record, payload.revision)
            if record["status"] != "pending":
                raise HTTPException(409, "仅待审核资料可调整优先级")
            record["priority"] = payload.priority
            session.put_publication(record)
            self._audit_action(
                session,
                {**record, "id": record["toolId"], "name": record["data"]["name"]},
                actor,
                "调整审核优先级",
            )
            return record

    def listing(self, tool_id: str, payload: ToolListing, actor: Actor) -> dict:
        """原子更新上下架并返回落库后的修订，支持页面连续管理操作。"""
        with self.repository.transaction() as session:
            resource = self._resource(session, tool_id, actor, True)
            ensure_revision(resource, payload.revision)
            resource.update(listingStatus=payload.status, revision=resource["revision"] + 1)
            session.put_resource(resource)
            self._audit_action(
                session, resource, actor, "上架" if payload.status == "listed" else "下架"
            )
        return self.resource(tool_id, actor, True)

    def visibility(self, tool_id: str, payload: ToolVisibility, actor: Actor) -> dict:
        """按修订号保存任务约定的开放范围，权限目录校验复用公共逻辑。"""
        with self.repository.transaction() as session:
            resource = self._resource(session, tool_id, actor, True)
            ensure_revision(session.get_visibility(tool_id), payload.revision)
            value = {**payload.model_dump(by_alias=True), "revision": payload.revision + 1}
            session.put_visibility(tool_id, value)
            self._audit_action(session, resource, actor, "调整可见范围")
            return value

    def _audit_action(
        self, session: ResourceSession, resource: dict, actor: Actor, action: str
    ) -> None:
        """保留操作者身份，确保持久化审计归属于实际管理用户。"""
        event = {
            "id": uuid4().hex,
            "toolId": resource["id"],
            "name": resource["name"],
            "organizationId": resource["organizationId"],
            "reviewer": actor.name,
            "actorId": actor.id,
            "actorName": actor.name,
            "decision": action,
            "reason": "",
            "createdAt": timestamp(),
        }
        session.put_entry("audit", event["id"], event)

    def audit(self, query: ToolAuditQuery, actor: Actor) -> dict:
        """按管理范围分页查看审核与上下架历史。"""
        require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            rows = [
                item
                for item in session.entries("audit")
                if can_manage(actor, item)
                and query.q.casefold() in (item["name"] + item["reviewer"]).casefold()
                and (not query.reviewer or query.reviewer.casefold() in item["reviewer"].casefold())
                and (not query.decision or query.decision == item["decision"])
                and (not query.start or item["createdAt"][:10] >= query.start.isoformat())
                and (not query.end or item["createdAt"][:10] <= query.end.isoformat())
            ]
            rows.sort(key=lambda item: item["createdAt"], reverse=True)
            return paginate(rows, query.page, query.page_size)

    def interact(self, tool_id: str, actor: Actor, **changes: object) -> dict:
        """每账号每工具一份互动记录，重复收藏与修改评分不会重复计数。"""
        with self.repository.transaction() as session:
            resource = self._resource(session, tool_id, actor)
            key = actor.id + ":" + tool_id
            record = session.entry("interactions", key) or {"actorId": actor.id, "toolId": tool_id}
            session.put_entry("interactions", key, {**record, **changes})
            return public_tool(self._summary(session, resource, actor))

    def invoke(self, tool_id: str, payload: InvokeTool, actor: Actor) -> dict:
        """执行前校验输入和可见范围，成功与失败都记录本次运行耗时。"""
        with self.repository.transaction(is_readonly=True) as session:
            resource = self._resource(session, tool_id, actor)
        validate_schema_input(resource["inputSchema"], payload.inputs, "工具输入")
        if payload.simulate_failure and self.provider.mode != "simulation":
            raise HTTPException(422, "当前工具服务不支持模拟失败")
        started = perf_counter()
        error = None
        output = None
        try:
            output = self.provider.invoke(resource, payload.inputs, payload.simulate_failure)
            try:
                validate_schema_input(resource["outputSchema"], output, "工具输出")
            except HTTPException as cause:
                raise HTTPException(
                    502, "工具输出不符合声明的 JSON Schema，请检查输出示例或供应方适配器"
                ) from cause
        except HTTPException as cause:
            error = cause
        except Exception:
            error = HTTPException(502, "工具服务未能完成调用，请稍后重试")
        run = {
            "id": "tool-run-" + uuid4().hex[:12],
            "toolId": tool_id,
            "actorId": actor.id,
            "version": resource["version"],
            "mode": self.provider.mode,
            "createdAt": timestamp(),
            "success": error is None,
            "durationMs": round((perf_counter() - started) * 1000, 2),
        }
        with self.repository.transaction() as session:
            session.put_entry("runs", run["id"], run)
        if error:
            raise error
        text = output.get("result", output) if isinstance(output, dict) else output
        return {**run, "output": output, "text": str(text)}

    def import_tool(self, external_id: str, actor: Actor, defaults: dict) -> dict:
        """工具链导入只创建草稿；重复导入同一版本返回本人已有申请。"""
        candidate = self.provider.get_tool(external_id)
        # 重复导入检查、草稿和关联标识在同一个事务内提交。
        with self.repository.transaction() as session:
            existing = next(
                (
                    item
                    for item in session.list_publications()
                    if item["createdBy"] == actor.id
                    and item.get("source", {}).get("externalId", item.get("externalId"))
                    == external_id
                    and item["data"]["version"] == candidate["version"]
                ),
                None,
            )
            if existing:
                return existing
            fields = {field.alias or name for name, field in ToolInput.model_fields.items()}
            data = ToolInput.model_validate(
                {key: value for key, value in candidate.items() if key in fields}
            )
            previous = next(
                (
                    item
                    for item in session.list_resources()
                    if item["createdBy"] == actor.id
                    and item.get("source", {}).get("externalId") == external_id
                ),
                None,
            )
            if previous and any(
                item["toolId"] == previous["id"] and item["status"] != "approved"
                for item in session.list_publications()
            ):
                raise HTTPException(409, "该工具已有待发布版本，请先处理已有申请")
            if previous and candidate["version"] in {
                item["version"] for item in previous["versions"]
            }:
                raise HTTPException(409, "该版本已发布，请在工具链创建新版本后再导入")
            record = self._prepare_publication(session, SaveTool(data=data), actor, defaults)
            if previous:
                record.update(toolId=previous["id"], baseVersion=previous["version"])
            record["externalId"] = external_id
            record["data"]["version"] = candidate["version"]
            record["source"] = {
                "kind": "toolchain",
                "label": "工具链导入",
                "externalId": external_id,
                "syncedAt": timestamp(),
                "status": "synced",
            }
            session.put_publication(record)
            return record

    def definition(self, tool_id: str, actor: Actor) -> dict:
        """向智能体工具链提供当前可见工具的调用契约，执行仍统一经过平台权限校验。"""
        resource = self.resource(tool_id, actor)
        return {
            "toolId": tool_id,
            "version": resource["version"],
            "name": resource["name"],
            "description": resource["description"],
            "inputSchema": json.loads(resource["inputSchema"]),
            "outputSchema": json.loads(resource["outputSchema"]),
            "invocation": {
                "method": "POST",
                "path": f"/api/v1/tools/{tool_id}/invoke",
                "inputField": "inputs",
                "authentication": "platform_session",
            },
            "mode": self.provider.mode,
        }


def tool_workspace(request: Request) -> ToolWorkspace:
    """通过应用实例取得隔离的工具业务对象。"""
    return request.app.state.tool_workspace
