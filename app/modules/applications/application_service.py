"""场景应用业务流程：与存储实现、供应方服务分离。"""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request

from app.modules.applications.configuration import (
    SaveApplicationConfiguration,
    configuration_view,
    validate_submission,
)
from app.modules.applications.identity import (
    DEFAULT_ADMIN,
    DEFAULT_USER,
    can_edit,
    can_manage,
    require_admin,
    visible,
)
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


def timestamp():
    return datetime.now(UTC).isoformat(timespec="microseconds")


def application_resource(
    data: dict, application_id: str, version_id: str, created_at: str, now: str
):
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


# 路由接收 HTTP，本类处理业务规则；repository 保存数据，provider 提供关联资源。
class ApplicationService:
    def __init__(self, repository: ApplicationRepository, provider: ApplicationProvider):
        self.repository = repository
        self.provider = provider
        self.workflows = ApplicationWorkflows(self)

    @staticmethod
    def _resource(session: ApplicationSession, application_id: str):
        result = session.get_resource(application_id)
        if result is None:
            raise HTTPException(404, "应用不存在或尚未上架")
        return result

    @staticmethod
    def _publication(session: ApplicationSession, publication_id: str):
        result = session.get_publication(publication_id)
        if result is None:
            raise HTTPException(404, "申请不存在，请重新查询")
        return result

    def resources(self, actor=DEFAULT_USER, *, admin=False):
        if admin:
            require_admin(actor)
        with self.repository.transaction() as session:
            return [
                item
                for item in session.list_resources()
                if (
                    can_manage(actor, item)
                    if admin
                    else visible(actor, item, session.get_visibility(item["id"]))
                )
            ]

    def resource(self, application_id: str, actor=DEFAULT_USER):
        with self.repository.transaction() as session:
            resource = self.workflows.public_resource(session, application_id, actor)
            return {**resource, "canPublishVersion": can_edit(actor, resource)}

    def publications(self, actor=DEFAULT_USER, *, admin=False):
        if admin:
            require_admin(actor)
        with self.repository.transaction() as session:
            return sorted(
                [
                    item
                    for item in session.list_publications()
                    if (can_manage(actor, item) if admin else can_edit(actor, item))
                ],
                key=lambda item: item["updatedAt"],
                reverse=True,
            )

    def publication(self, publication_id: str, actor=DEFAULT_USER, *, admin=False):
        if admin:
            require_admin(actor)
        with self.repository.transaction() as session:
            record = self._publication(session, publication_id)
            if not (can_manage(actor, record) if admin else can_edit(actor, record)):
                raise HTTPException(404, "申请不存在或当前账号无权查看")
            return record

    def create_version(self, application_id: str, actor=DEFAULT_USER):
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
            record = self._save(session, SaveApplicationPublication(data=data), actor=actor)
            for key in ("createdBy", "createdByName", "organizationId", "source", "securityLevel"):
                if key in source:
                    record[key] = deepcopy(source[key])
            record["applicationId"] = application_id
            record["baseVersionId"] = source["versionId"]
            session.put_publication(record)
            return record

    def _validate_version(self, session: ApplicationSession, data: dict, record: dict):
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

    # 【场景应用交互 5】处理并返回：接收路由传入的 payload，在一次仓储事务中保存申请。
    # 返回值逐层交回 router.py，最终成为前端 save() 成功回调中的 result。
    def save(
        self,
        payload: SaveApplicationPublication,
        publication_id: str | None = None,
        actor=DEFAULT_USER,
    ):
        with self.repository.transaction() as session:
            return self._save(session, payload, publication_id, actor)

    def _save(
        self,
        session: ApplicationSession,
        payload: SaveApplicationPublication,
        publication_id: str | None = None,
        actor=DEFAULT_USER,
    ):
        if actor.role not in ("publisher", "org_admin", "admin"):
            raise HTTPException(403, "当前账号没有发布应用的权限")
        # 参数对象转成字典；by_alias=True 保持前端使用的 camelCase 字段名。
        data = payload.data.model_dump(by_alias=True)
        self.provider.validate_dependencies(data["dependencies"])
        configuration = session.get_configuration()
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
        self, publication_id: str, payload: ReviewApplicationPublication, actor=DEFAULT_ADMIN
    ):
        require_admin(actor)
        with self.repository.transaction() as session:
            record = self._publication(session, publication_id)
            if not can_manage(actor, record):
                raise HTTPException(403, "只能审核本单位的申请")
            if record["revision"] != payload.revision or record["status"] != "pending":
                raise HTTPException(409, "申请状态已变化，请刷新后审核")
            if payload.decision in ("rejected", "needs_changes") and not payload.reason:
                raise HTTPException(422, "驳回或要求补充材料时请填写原因")
            if payload.decision == "approved":
                if payload.scores is None:
                    raise HTTPException(422, "通过前请完成完整性、可用性和安全性评分")
                threshold = self.workflows.policy_in(session)["minimumQualityScore"]
                if min(payload.scores.model_dump().values()) < threshold:
                    raise HTTPException(422, "质量评分未达到通过阈值，请要求补充材料或驳回")
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
                    "scores": payload.scores.model_dump() if payload.scores else None,
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
        self, application_id: str, payload: ApplicationVisibility | None = None, actor=DEFAULT_ADMIN
    ):
        require_admin(actor)
        with self.repository.transaction() as session:
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

    def admin_resource(self, application_id: str, actor=DEFAULT_ADMIN):
        require_admin(actor)
        with self.repository.transaction() as session:
            resource = self._resource(session, application_id)
            if not can_manage(actor, resource):
                raise HTTPException(403, "只能管理本单位应用")
            resource["visibility"] = session.get_visibility(application_id)
        resource["runtime"] = self.workflows.runtime(
            application_id
        ) or self.provider.runtime_status(application_id, resource["versionId"])
        return resource

    def configuration(self, payload: SaveApplicationConfiguration | None = None, *, public=False):
        with self.repository.transaction() as session:
            current = session.get_configuration()
            if payload is not None:
                if payload.revision != current["revision"]:
                    raise HTTPException(409, "配置已发生变化，请重新加载后修改")
                current = {
                    **payload.model_dump(by_alias=True),
                    "revision": current["revision"] + 1,
                    "updatedAt": timestamp(),
                }
                session.put_configuration(current)
            return configuration_view(current, public=public)


# 取出启动时创建的业务实例；同一后端实例的多次请求共用它及其仓储。
def application_service(request: Request) -> ApplicationService:
    return request.app.state.application_service
