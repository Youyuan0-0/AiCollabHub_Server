"""场景应用业务流程：与存储实现、供应方服务分离。"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request

from app.modules.applications.configuration import (
    SaveApplicationConfiguration,
    configuration_view,
    effective_configuration,
    validate_submission,
)
from app.modules.applications.discovery import analyze
from app.modules.applications.ports import (
    ApplicationProvider,
    ApplicationRepository,
    ApplicationSession,
)
from app.modules.applications.publication_schemas import (
    ApplicationDetails,
    ApplicationVisibility,
    ReviewApplicationPublication,
    SaveApplicationPublication,
)
from app.modules.applications.workflows import ApplicationWorkflows
from app.modules.common.identity import (
    DEFAULT_ADMIN,
    DEFAULT_USER,
    Actor,
    can_edit,
    can_manage,
    require_admin,
    visible,
)


def timestamp() -> str:
    """生成 UTC 时间文本，统一保留微秒精度。"""
    return datetime.now(UTC).isoformat(timespec="microseconds")


def application_resource(
    data: dict, application_id: str, version_id: str, created_at: str, now: str
) -> dict:
    """将已审核资料转换为目录资源，初始化版本和使用统计。"""
    return {
        **deepcopy(data),
        "id": application_id,
        "versionId": version_id,
        "kind": "application",
        "description": data["summary"],
        "usage": 0,
        "rating": None,
        "downloadCount": 0,
        "favoriteCount": 0,
        "lastDownloadedAt": None,
        "updatedAt": now,
        "createdAt": created_at,
        "status": "available",
        "tone": "blue",
        "serviceStatus": "not_connected",
        "publicationStatus": "published",
        "versions": [
            {
                "id": version_id,
                "version": data["version"],
                "releasedAt": now,
                "status": "current",
                "note": data.get("versionNotes") or "应用资料通过模拟审核。",
            }
        ],
        "facets": {
            "scenario": [data["category"]],
            "sourceType": ["自研应用"],
            "runtimeState": ["未接入"],
        },
    }


class ApplicationService:
    """处理应用发布、审核和配置；通过仓储与供应方接口访问外部资源。"""

    def __init__(self, repository: ApplicationRepository, provider: ApplicationProvider) -> None:
        self.repository = repository
        self.provider = provider
        self.workflows = ApplicationWorkflows(self)

    @staticmethod
    def _resource(session: ApplicationSession, application_id: str) -> dict:
        result = session.get_resource(application_id)
        if result is None:
            raise HTTPException(404, "应用不存在或尚未上架")
        return result

    @staticmethod
    def _publication(session: ApplicationSession, publication_id: str) -> dict:
        result = session.get_publication(publication_id)
        if result is None:
            raise HTTPException(404, "申请不存在，请重新查询")
        return result

    def resources(
        self,
        actor: Actor = DEFAULT_USER,
        *,
        is_admin: bool = False,
        warnings: list[str] | None = None,
    ) -> list[dict]:
        """列出有权查看的应用，并可向目录页面传递资料不完整的提示。"""
        if is_admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            items = [
                item
                for item in session.list_resources()
                if (
                    can_manage(actor, item)
                    if is_admin
                    else visible(actor, item, session.get_visibility(item["id"]))
                )
            ]
            if warnings is not None:
                warnings.extend(session.resource_warnings)
            return items

    def resource(self, application_id: str, actor: Actor = DEFAULT_USER) -> dict:
        """读取有权访问的应用详情，并附带运行连接状态和版本发布权限。"""
        with self.repository.transaction(is_readonly=True) as session:
            resource = self.workflows.public_resource(session, application_id, actor)
            connection = (
                self.provider.connection(resource)
                if hasattr(self.provider, "connection")
                else {"mode": "simulation", "configured": True, "message": "当前使用模拟运行服务"}
            )
            return {
                **resource,
                "canPublishVersion": can_edit(actor, resource),
                "runtimeConnection": connection,
            }

    def publications(
        self,
        actor: Actor = DEFAULT_USER,
        *,
        is_admin: bool = False,
        warnings: list[str] | None = None,
    ) -> list[dict]:
        """按更新时间倒序返回可编辑或可管理的发布申请。"""
        if is_admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            return sorted(
                [
                    item
                    for item in session.list_publications(warnings=warnings)
                    if (can_manage(actor, item) if is_admin else can_edit(actor, item))
                ],
                key=lambda item: item["updatedAt"],
                reverse=True,
            )

    def publication(
        self, publication_id: str, actor: Actor = DEFAULT_USER, *, is_admin: bool = False
    ) -> dict:
        """读取发布申请；无权查看时与记录不存在统一返回 404。"""
        if is_admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            record = self._publication(session, publication_id)
            if not (can_manage(actor, record) if is_admin else can_edit(actor, record)):
                raise HTTPException(404, "申请不存在或当前账号无权查看")
            return record

    def create_version(self, application_id: str, actor: Actor = DEFAULT_USER) -> dict:
        """为已有应用创建版本草稿；存在未通过的申请时复用该申请。"""
        with self.repository.transaction() as session:
            source = self.workflows.public_resource(session, application_id, actor)
            if not can_edit(actor, source):
                raise HTTPException(403, "只有发布者或所属单位管理员可以发布新版本")
            existing = next(
                (
                    item
                    for item in session.list_publications()
                    if item["applicationId"] == application_id and item["status"] != "approved"
                ),
                None,
            )
            if existing:
                return existing
            data = ApplicationDetails.model_validate(
                {
                    field.alias or name: source[field.alias or name]
                    for name, field in ApplicationDetails.model_fields.items()
                    if (field.alias or name) in source
                }
            )
            data.summary = source["description"]
            data.version = ""
            data.version_notes = ""
            # 旧演示目录使用中文密级；新版本沿用原密级并转换为表单枚举。
            security_level = source.get("securityLevel", "internal")
            security_level = {
                "内部": "internal",
                "公开": "public",
                "机密": "confidential",
            }.get(security_level, security_level)
            return self._save(
                session,
                SaveApplicationPublication(data=data, security_level=security_level),
                actor=actor,
                base_resource=source,
            )

    def _validate_version(self, session: ApplicationSession, data: dict, record: dict) -> None:
        source = session.get_resource(record["applicationId"])
        if not source:
            return
        if source["versionId"] != record.get("baseVersionId"):
            raise HTTPException(409, "应用版本已更新，请重新打开最新应用后发布新版本")
        if data["version"] and any(
            item["version"].casefold() == data["version"].casefold()
            for item in source.get("versions", [])
        ):
            raise HTTPException(422, "版本号已经发布，请使用新的版本号")

    def save(
        self,
        payload: SaveApplicationPublication,
        publication_id: str | None = None,
        actor: Actor = DEFAULT_USER,
    ) -> dict:
        """在一次事务中保存草稿或提交审核；旧 revision 不能覆盖新记录。"""
        with self.repository.transaction() as session:
            return self._save(session, payload, publication_id, actor)

    def _save(
        self,
        session: ApplicationSession,
        payload: SaveApplicationPublication,
        publication_id: str | None = None,
        actor: Actor = DEFAULT_USER,
        *,
        base_resource: dict | None = None,
    ) -> dict:
        if actor.role not in ("publisher", "org_admin", "admin"):
            raise HTTPException(403, "当前账号没有发布应用的权限")
        # 参数对象转成字典；by_alias=True 保持前端使用的 camelCase 字段名。
        data = payload.data.model_dump(by_alias=True)
        from app.modules.applications.tutorial_files import validate_files

        validate_files(session, data, actor, payload.security_level)
        if payload.auto_classify:
            suggestion = analyze(data)["suggestedCategory"]
            if suggestion:
                data["category"] = suggestion
        self.provider.validate_dependencies(data["dependencies"])
        configuration = effective_configuration(session.get_configuration())
        if payload.submit:
            # 后端再次检查当前必填规则，不能只依赖浏览器中的表单校验。
            validate_submission(data, configuration)
            self.workflows.validate_security(session, data, payload.security_level)
        now = timestamp()
        if publication_id:
            record = self._publication(session, publication_id)
            if not can_edit(actor, record):
                raise HTTPException(403, "不能修改其他发布者的申请")
            # 前端回传上次读取的 revision；不一致则返回 409，防止旧表单覆盖新记录。
            if record["revision"] != payload.revision:
                raise HTTPException(409, "申请已发生变化，请重新打开后操作")
            if record["status"] not in ("draft", "rejected", "needs_changes"):
                raise HTTPException(409, "待审核或已通过的申请不能修改")
            self._validate_version(session, data, record)
        else:
            record = {
                "id": f"publication-{uuid4()}",
                "applicationId": f"app-{uuid4()}",
                "versionId": f"version-{uuid4()}",
                "revision": 0,
                "createdAt": now,
                "reviews": [],
                "submittedAt": None,
                "baseVersionId": None,
                "createdBy": actor.id,
                "createdByName": actor.name,
                "organizationId": actor.organization_id,
                "source": {"kind": "manual"},
            }
            if base_resource is not None:
                for key in ("createdBy", "createdByName", "organizationId", "source"):
                    if key in base_resource:
                        record[key] = deepcopy(base_resource[key])
                record["applicationId"] = base_resource["id"]
                record["baseVersionId"] = base_resource["versionId"]
        # 保存草稿为 draft，提交审核为 pending；每次成功保存都增加 revision。
        record.update(
            {
                "data": data,
                "securityLevel": payload.security_level,
                "priority": payload.priority,
                "updatedAt": now,
                "revision": record["revision"] + 1,
                "status": "pending" if payload.submit else "draft",
            }
        )
        if payload.submit:
            record["submittedAt"] = now
            record["formRevision"] = configuration["revision"]
            record["submissionRules"] = configuration
        # 写入申请仓储并返回副本；审核通过时才会另行写入上架资源目录。
        session.put_publication(record)
        return deepcopy(record)

    def review(
        self,
        publication_id: str,
        payload: ReviewApplicationPublication,
        actor: Actor = DEFAULT_ADMIN,
    ) -> dict:
        """审核待处理申请，将审核结果、上架资源和通知在同一事务中保存。"""
        require_admin(actor)
        with self.repository.transaction() as session:
            record = self._publication(session, publication_id)
            if not can_manage(actor, record):
                raise HTTPException(403, "只能审核本单位的申请")
            if record["revision"] != payload.revision or record["status"] != "pending":
                raise HTTPException(409, "申请状态已变化，请刷新后审核")
            if payload.decision in ("rejected", "needs_changes") and not payload.reason:
                raise HTTPException(422, "驳回或要求补充材料时请填写原因")
            from app.modules.applications.review_files import public_report, validate_reports

            reports = validate_reports(session, record, payload.report_file_ids, actor)
            if payload.decision == "approved":
                self.workflows.validate_security(
                    session, record["data"], record.get("securityLevel", "internal")
                )
                self._validate_version(session, record["data"], record)
                self.provider.validate_dependencies(record["data"]["dependencies"])
            now = timestamp()
            record["reviews"].append(
                {
                    "id": f"review-{uuid4()}",
                    "decision": payload.decision,
                    "reason": payload.reason,
                    "reviewerId": actor.id,
                    "reviewerName": actor.name,
                    "scores": payload.scores.model_dump(by_alias=True),
                    "reportFiles": [public_report(file) for file in reports],
                    "reviewedAt": now,
                    "submittedAt": record["submittedAt"],
                    "data": deepcopy(record["data"]),
                }
            )
            record.update(
                {"status": payload.decision, "updatedAt": now, "revision": record["revision"] + 1}
            )
            if payload.decision == "approved":
                previous = session.get_resource(record["applicationId"])
                resource = application_resource(
                    record["data"],
                    record["applicationId"],
                    record["versionId"],
                    record["createdAt"],
                    now,
                )
                resource.update(
                    createdBy=record.get("createdBy", DEFAULT_USER.id),
                    organizationId=record.get("organizationId", "org-research"),
                    source=record.get("source", {"kind": "manual"}),
                    securityLevel=record.get("securityLevel", "internal"),
                )
                if resource["source"]["kind"] == "toolchain":
                    resource["facets"]["sourceType"] = ["工具链应用"]
                if previous:
                    for key in ("createdBy", "createdByName", "organizationId"):
                        if key in previous:
                            resource[key] = previous[key]
                    resource["createdAt"] = previous["createdAt"]
                    resource["usage"] = previous["usage"]
                    resource["rating"] = previous["rating"]
                    resource["ratingCount"] = previous.get("ratingCount", 0)
                    for metric in ("downloadCount", "favoriteCount", "lastDownloadedAt"):
                        resource[metric] = previous[metric]
                    resource["versions"].extend(
                        {
                            **version,
                            "status": "available"
                            if version["status"] == "current"
                            else version["status"],
                        }
                        for version in previous.get("versions", [])
                    )
                # 审核通过：把资料加入应用目录，与申请状态一起提交；用户端重新查询后显示。
                session.put_resource(resource)
            session.put_publication(record)
            for file in reports:
                session.put_entry(
                    "review_files", file["id"], {**file, "reviewId": record["reviews"][-1]["id"]}
                )
            application = {"id": record["applicationId"], "name": record["data"]["name"]}
            labels = {"approved": "已通过", "rejected": "已驳回", "needs_changes": "需补充材料"}
            self.workflows.notify(
                session,
                record.get("createdBy", DEFAULT_USER.id),
                application,
                labels[payload.decision],
                "review",
                f"review:{record['reviews'][-1]['id']}",
            )
            if payload.decision == "approved":
                for item in session.entries("interactions"):
                    if item["applicationId"] == record["applicationId"] and item["subscribed"]:
                        self.workflows.notify(
                            session,
                            item["actorId"],
                            application,
                            f"已发布 {record['data']['version']}",
                            "new_version",
                            f"version:{record['versionId']}:{item['actorId']}",
                        )
            return deepcopy(record)

    def visibility(
        self,
        application_id: str,
        payload: ApplicationVisibility | None = None,
        actor: Actor = DEFAULT_ADMIN,
    ) -> dict:
        """读取或修改有权管理的应用开放范围；传入 revision 时检查并发冲突。"""
        require_admin(actor)
        with self.repository.transaction(is_readonly=payload is None) as session:
            resource = self._resource(session, application_id)
            if not can_manage(actor, resource):
                raise HTTPException(403, "只能配置本单位应用")
            current = session.get_visibility(application_id)
            if payload is not None:
                if payload.revision is not None and payload.revision != current["revision"]:
                    raise HTTPException(409, "开放范围已被修改，请刷新后操作")
                if payload.scope != "all" and not payload.targets:
                    raise HTTPException(422, "请选择可见的组织或成员")
                current = {
                    "scope": payload.scope,
                    "targets": payload.targets if payload.scope != "all" else [],
                    "revision": current["revision"] + 1,
                }
                session.put_visibility(application_id, current)
            return current

    def admin_resource(self, application_id: str, actor: Actor = DEFAULT_ADMIN) -> dict:
        """读取管理详情，包含开放范围和运行状态，并校验单位管理权限。"""
        require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            resource = self._resource(session, application_id)
            if not can_manage(actor, resource):
                raise HTTPException(403, "只能管理本单位应用")
            resource["visibility"] = session.get_visibility(application_id)
        resource["runtime"] = self.workflows.runtime(
            application_id
        ) or self.provider.runtime_status(application_id, resource["versionId"])
        return resource

    def configuration(
        self,
        payload: SaveApplicationConfiguration | None = None,
        *,
        public: bool = False,
        actor: Actor | None = None,
    ) -> dict:
        """读取或按 revision 更新表单配置；public 视图只返回发布表单所需字段。"""
        with self.repository.transaction(is_readonly=payload is None) as session:
            current = session.get_configuration()
            if payload is not None:
                if payload.revision != current["revision"]:
                    raise HTTPException(409, "配置已发生变化，请重新加载后修改")
                current = {
                    **payload.model_dump(by_alias=True),
                    "revision": current["revision"] + 1,
                    "updatedAt": timestamp(),
                }
                if actor is not None:
                    current["updatedBy"] = actor.id
                session.put_configuration(current)
            return configuration_view(
                {key: value for key, value in current.items() if key != "updatedBy"}, public=public
            )


def application_service(request: Request) -> ApplicationService:
    """取得启动时创建的业务实例，同一后端进程内复用实例及其仓储。"""
    return request.app.state.application_service
