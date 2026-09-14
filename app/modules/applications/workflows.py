"""应用运行、个人操作、同步及审核扩展；全部通过仓储和供应方合同。"""

import json
from datetime import UTC, datetime
from time import perf_counter

from fastapi import HTTPException

from app.modules.applications.configuration import validate_submission
from app.modules.applications.identity import can_manage, require_admin, visible
from app.modules.applications.publication_schemas import (
    APPLICATION_CATEGORIES,
    ApplicationDetails,
    SaveApplicationPublication,
)
from app.modules.applications.workflow_schemas import WorkflowPolicy
from app.modules.common.fixtures import paginate


def now():
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ApplicationWorkflows:
    def __init__(self, service):
        self.service = service
        self.repository = service.repository
        self.provider = service.provider

    @staticmethod
    def policy_in(session):
        return session.entry("policy", "current") or WorkflowPolicy(revision=1).model_dump(
            by_alias=True
        )

    def policy(self, actor=None, payload=None):
        if actor:
            require_admin(actor)
        with self.repository.transaction() as session:
            current = self.policy_in(session)
            if payload:
                if actor.role != "admin":
                    raise HTTPException(403, "只有系统管理员可以修改应用规则")
                if payload.revision != current["revision"]:
                    raise HTTPException(409, "规则已更新，请重新加载")
                ids = {item["id"] for item in session.list_resources()}
                if set(payload.recommended_ids) - ids or set(payload.hot_categories) - set(
                    APPLICATION_CATEGORIES
                ):
                    raise HTTPException(422, "推荐应用或热门分类不存在")
                current = {**payload.model_dump(by_alias=True), "revision": current["revision"] + 1}
                session.put_entry("policy", "current", current)
            if actor:
                return current
            return {
                key: current[key]
                for key in (
                    "revision",
                    "announcement",
                    "recommendedIds",
                    "hotCategories",
                    "defaultSort",
                    "pageSize",
                    "toolchainUrl",
                )
            }

    def public_resource(self, session, application_id, actor):
        resource = self.service._resource(session, application_id)
        if not visible(actor, resource, session.get_visibility(application_id)):
            raise HTTPException(404, "应用不存在或当前账号不可见")
        return resource

    def notify(self, session, recipient, application, status, kind, key):
        policy = self.policy_in(session)
        if not policy["notificationsEnabled"] or session.entry("notifications", key):
            return
        session.put_entry(
            "notifications",
            key,
            {
                "id": key,
                "recipient": recipient,
                "applicationId": application["id"],
                "title": policy["notificationTemplate"].format(
                    name=application["name"], status=status
                ),
                "kind": kind,
                "createdAt": now(),
                "read": False,
            },
        )

    def validate_security(self, session, data, security_level):
        if security_level == "confidential":
            raise HTTPException(422, "当前发布通道不接收涉密资料，请调整密级或走专用审批通道")
        text = json.dumps(data, ensure_ascii=False).casefold()
        if any(term.casefold() in text for term in self.policy_in(session)["sensitiveTerms"]):
            raise HTTPException(422, "资料命中发布拦截词，请检查并修改后提交")

    def quality(self, record, actor):
        require_admin(actor)
        if not can_manage(actor, record):
            raise HTTPException(403, "只能审核本单位的申请")
        with self.repository.transaction() as session:
            checks = []
            for label, check in (
                (
                    "必填与分类",
                    lambda: validate_submission(
                        record["data"], record.get("submissionRules", session.get_configuration())
                    ),
                ),
                (
                    "关联资源有效性",
                    lambda: self.provider.validate_dependencies(record["data"]["dependencies"]),
                ),
                (
                    "敏感词与密级",
                    lambda: self.validate_security(
                        session, record["data"], record.get("securityLevel", "internal")
                    ),
                ),
            ):
                try:
                    check()
                    checks.append({"name": label, "passed": True, "message": "校验通过"})
                except HTTPException as error:
                    checks.append({"name": label, "passed": False, "message": error.detail})
            optional = [
                ("详细说明", "descriptionMarkdown"),
                ("使用教程", "tutorials"),
                ("使用限制", "usageLimits"),
                ("输入格式", "inputSchema"),
                ("输出格式", "outputSchema"),
            ]
            suggestions = [f"建议补充{label}" for label, key in optional if not record["data"][key]]
            return {
                "checkedAt": now(),
                "checks": checks,
                "suggestions": suggestions,
                "minimumScore": self.policy_in(session)["minimumQualityScore"],
                "basis": "本平台资料规则检查；不代表实际模型效果评测",
            }

    def interaction(self, application_id, actor, payload=None):
        with self.repository.transaction() as session:
            resource = self.public_resource(session, application_id, actor)
            key = f"{actor.id}:{application_id}"
            value = session.entry("interactions", key) or {
                "id": key,
                "actorId": actor.id,
                "applicationId": application_id,
                "favorite": False,
                "subscribed": False,
                "rating": None,
                "comment": "",
            }
            if payload:
                if payload.favorite is not None and payload.favorite != value["favorite"]:
                    resource["favoriteCount"] = max(
                        0, resource["favoriteCount"] + (1 if payload.favorite else -1)
                    )
                    value["favorite"] = payload.favorite
                if payload.subscribed is not None:
                    value["subscribed"] = payload.subscribed
                if payload.rating is not None:
                    value.update(rating=payload.rating, comment=payload.comment)
                value["updatedAt"] = now()
                session.put_entry("interactions", key, value)
                ratings = [
                    item["rating"]
                    for item in session.entries("interactions")
                    if item["applicationId"] == application_id and item["rating"] is not None
                ]
                if ratings:
                    resource["rating"] = round(sum(ratings) / len(ratings), 2)
                    resource["ratingCount"] = len(ratings)
                session.put_resource(resource)
            return {
                **value,
                "ratingAverage": resource["rating"],
                "ratingCount": resource.get("ratingCount", 0),
                "favoriteCount": resource["favoriteCount"],
            }

    def ratings(self, application_id, actor):
        with self.repository.transaction() as session:
            self.public_resource(session, application_id, actor)
            return [
                {
                    "id": item["id"],
                    "rating": item["rating"],
                    "comment": item["comment"],
                    "updatedAt": item["updatedAt"],
                }
                for item in session.entries("interactions")
                if item["applicationId"] == application_id and item["rating"] is not None
            ]

    def activity(self, actor):
        with self.repository.transaction() as session:
            available = {
                item["id"]: item
                for item in session.list_resources()
                if visible(actor, item, session.get_visibility(item["id"]))
            }
            interactions = [
                item for item in session.entries("interactions") if item["actorId"] == actor.id
            ]
            subscriptions = [
                {**available[item["applicationId"]], "subscribedAt": item.get("updatedAt")}
                for item in interactions
                if item["subscribed"] and item["applicationId"] in available
            ]
            favorites = [
                item["applicationId"]
                for item in interactions
                if item["favorite"] and item["applicationId"] in available
            ]
            runs = [
                item
                for item in session.entries("runs")
                if item["actorId"] == actor.id and item["applicationId"] in available
            ]
            notifications = [
                item
                for item in session.entries("notifications")
                if (
                    item["recipient"] == actor.id
                    or (item["recipient"] == "@system-admin" and actor.role == "admin")
                )
                and (item["applicationId"] in available or item["kind"] == "review")
            ]
            return {
                "subscriptions": subscriptions,
                "favoriteIds": favorites,
                "runs": sorted(
                    [self.run_view(item) for item in runs],
                    key=lambda item: item["createdAt"],
                    reverse=True,
                ),
                "notifications": sorted(
                    notifications, key=lambda item: item["createdAt"], reverse=True
                ),
            }

    def read_notification(self, notification_id, actor):
        with self.repository.transaction() as session:
            item = session.entry("notifications", notification_id)
            if not item or item["recipient"] != actor.id:
                raise HTTPException(404, "通知不存在")
            item["read"] = True
            session.put_entry("notifications", notification_id, item)
            return item

    def download(self, application_id, actor, request_id):
        with self.repository.transaction() as session:
            resource = self.public_resource(session, application_id, actor)
            key = f"{actor.id}:{application_id}:{request_id}"
            previous = session.entry("downloads", key)
            if previous:
                return previous
            data = ApplicationDetails.model_validate(
                {
                    field.alias or name: resource[field.alias or name]
                    for name, field in ApplicationDetails.model_fields.items()
                    if (field.alias or name) in resource
                }
            )
            value = {
                "filename": f"{application_id}-{resource['versionId']}.json",
                "content": data.model_dump(by_alias=True),
                "downloadedAt": now(),
            }
            value["content"]["contact"] = ""
            resource["downloadCount"] += 1
            resource["lastDownloadedAt"] = value["downloadedAt"]
            session.put_resource(resource)
            session.put_entry("downloads", key, value)
            return value

    def run(self, application_id, actor, payload):
        key = f"{actor.id}:{application_id}:{payload.request_id}"
        fingerprint = json.dumps(
            payload.model_dump(exclude={"request_id"}), sort_keys=True, ensure_ascii=False
        )
        if len(fingerprint) > 60000:
            raise HTTPException(422, "输入内容过长")
        with self.repository.transaction() as session:
            resource = self.public_resource(session, application_id, actor)
            previous = session.entry("runs", key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "同一请求编号不能用于不同输入")
                return self.run_view(previous)
            self.validate_security(
                session, {"input": payload.input, "options": payload.options}, "internal"
            )
            allowed = {item["name"] for item in resource["configOptions"]}
            if set(payload.options) - allowed or any(
                len(value) > 2000 for value in payload.options.values()
            ):
                raise HTTPException(422, "包含无效应用配置项")
            self.validate_input(resource.get("inputSchema", ""), payload.input)
            record = {
                "id": key,
                "actorId": actor.id,
                "applicationId": application_id,
                "applicationName": resource["name"],
                "versionId": resource["versionId"],
                "version": resource["version"],
                "input": payload.input,
                "options": payload.options,
                "fingerprint": fingerprint,
                "status": "running",
                "createdAt": now(),
                "output": None,
                "error": "",
                "durationMs": None,
                "mode": "simulation",
            }
            session.put_entry("runs", key, record)
        started = perf_counter()
        try:
            output = self.provider.invoke(
                resource, payload.input, payload.options, payload.simulate_failure
            )
            record.update(status="succeeded", output=output)
        except Exception:
            record.update(status="failed", error="服务调用失败，可以重新发起；本次失败已记录")
        record.update(
            durationMs=max(1, round((perf_counter() - started) * 1000)), completedAt=now()
        )
        with self.repository.transaction() as session:
            session.put_entry("runs", key, record)
            current = self.service._resource(session, application_id)
            if record["status"] == "succeeded":
                current["usage"] += 1
                session.put_resource(current)
            failures = sum(
                item["status"] == "failed" and item["applicationId"] == application_id
                for item in session.entries("runs")
            )
            threshold = self.policy_in(session)["failureThreshold"]
            if record["status"] == "failed" and failures % threshold == 0:
                self.notify(
                    session,
                    "@system-admin",
                    current,
                    f"累计调用失败 {failures} 次",
                    "run_failure",
                    f"failure:{application_id}:{failures}",
                )
        # 运行期间开放范围可能变化，结果返回也需要重新检查。
        self.service.resource(application_id, actor)
        return self.run_view(record)

    @staticmethod
    def run_view(record):
        return {key: value for key, value in record.items() if key != "fingerprint"}

    @staticmethod
    def validate_input(schema_text, inputs):
        if not schema_text:
            if not inputs:
                raise HTTPException(422, "请填写应用输入")
            return
        from jsonschema import Draft202012Validator
        from referencing import Registry

        try:
            schema = json.loads(schema_text)
            Draft202012Validator.check_schema(schema)
            # 不读取任何远程引用，避免应用元数据触发任意网络访问。
            validator = Draft202012Validator(schema, registry=Registry())
            errors = list(validator.iter_errors(inputs))
            if errors:
                raise HTTPException(422, "输入不符合应用格式：" + errors[0].message[:300])
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                422, "应用输入格式无法校验，请联系发布者检查 Schema 或外部引用"
            ) from None

    def runtime(self, application_id):
        with self.repository.transaction() as session:
            completed = [
                item
                for item in session.entries("runs")
                if item["applicationId"] == application_id
                and item["status"] in ("succeeded", "failed")
            ]
        if not completed:
            return None
        return {
            "status": "simulation",
            "averageResponseMs": round(
                sum(item["durationMs"] for item in completed) / len(completed), 1
            ),
            "successRate": sum(item["status"] == "succeeded" for item in completed)
            / len(completed),
            "sampleCount": len(completed),
            "measuredAt": max(item["completedAt"] for item in completed),
        }

    def sync(self, actor, payload):
        if actor.role not in ("publisher", "admin", "org_admin"):
            raise HTTPException(403, "当前账号不能导入工具链应用")
        key = f"{actor.id}:{payload.request_id}"
        binding_key = f"demo-toolchain:{payload.external_id}"
        fingerprint = f"{payload.external_id}:{payload.external_version}:{payload.simulate_failure}"
        with self.repository.transaction() as session:
            existing = session.entry("syncs", key)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "同步请求编号已用于其他资源")
                return existing
            job = {
                "id": key,
                "actorId": actor.id,
                "organizationId": actor.organization_id,
                "source": "demo-toolchain",
                "externalId": payload.external_id,
                "externalVersion": payload.external_version,
                "createdAt": now(),
                "status": "running",
                "fingerprint": fingerprint,
                "publicationId": None,
                "message": "",
                "mapping": [],
            }
            session.put_entry("syncs", key, job)
        try:
            if payload.simulate_failure:
                raise HTTPException(502, "模拟工具链暂时不可用，请重试")
            source = self.provider.toolchain_resource(payload.external_id, payload.external_version)
            with self.repository.transaction() as session:
                binding = session.entry("bindings", binding_key)
                if binding:
                    resource = session.get_resource(binding["applicationId"])
                    if binding["createdBy"] != actor.id and not can_manage(actor, binding):
                        raise HTTPException(403, "该工具链资源已由其他发布者登记")
                    records = [
                        item
                        for item in session.list_publications()
                        if item["applicationId"] == binding["applicationId"]
                    ]
                    matching = next(
                        (
                            item
                            for item in records
                            if item.get("source", {}).get("externalVersion")
                            == payload.external_version
                        ),
                        None,
                    )
                    if matching:
                        job.update(
                            status="unchanged",
                            publicationId=matching["id"],
                            message="该外部版本已登记，沿用现有申请",
                        )
                    elif any(item["status"] != "approved" for item in records):
                        raise HTTPException(409, "该应用有未完成的申请，请先处理后再同步新版本")
                    elif resource:
                        record = self.service._save(
                            session,
                            SaveApplicationPublication(
                                data=ApplicationDetails.model_validate(source["data"])
                            ),
                            actor=actor,
                        )
                        for field in (
                            "createdBy",
                            "createdByName",
                            "organizationId",
                            "securityLevel",
                        ):
                            if field in resource:
                                record[field] = resource[field]
                        record.update(
                            applicationId=resource["id"], baseVersionId=resource["versionId"]
                        )
                        self.service._validate_version(session, record["data"], record)
                    else:
                        raise HTTPException(409, "工具链绑定的应用状态异常，请管理员检查")
                else:
                    record = self.service._save(
                        session,
                        SaveApplicationPublication(
                            data=ApplicationDetails.model_validate(source["data"])
                        ),
                        actor=actor,
                    )
                    binding = {
                        "applicationId": record["applicationId"],
                        "createdBy": actor.id,
                        "organizationId": actor.organization_id,
                    }
                if job["status"] != "unchanged":
                    record["source"] = {
                        "kind": "toolchain",
                        "provider": "demo-toolchain",
                        "workspace": source["workspace"],
                        "externalId": payload.external_id,
                        "externalVersion": payload.external_version,
                        "syncedAt": now(),
                    }
                    session.put_publication(record)
                    session.put_entry("bindings", binding_key, binding)
                    job.update(
                        status="succeeded",
                        publicationId=record["id"],
                        message="已导入为草稿，请核对后提交审核",
                        mapping=[
                            {"source": key, "target": key, "status": "mapped"}
                            for key in source["data"]
                        ],
                    )
                job["completedAt"] = now()
                session.put_entry("syncs", key, job)
        except Exception as error:
            job.update(
                status="failed",
                message=error.detail
                if isinstance(error, HTTPException)
                else "工具链资料读取或映射失败",
                completedAt=now(),
            )
            with self.repository.transaction() as session:
                session.put_entry("syncs", key, job)
        return job

    def syncs(self, actor, admin=False):
        if admin:
            require_admin(actor)
        with self.repository.transaction() as session:
            return sorted(
                [
                    item
                    for item in session.entries("syncs")
                    if (can_manage(actor, item) if admin else item["actorId"] == actor.id)
                ],
                key=lambda item: item["createdAt"],
                reverse=True,
            )

    def history(self, actor, query):
        records = self.service.publications(actor, admin=True)
        items = []
        for record in records:
            for review in record["reviews"]:
                moment = datetime.fromisoformat(review["reviewedAt"]).date()
                if (
                    (query.reviewer and query.reviewer != review.get("reviewerId"))
                    or (query.decision and query.decision != review["decision"])
                    or (query.start and moment < query.start)
                    or (query.end and moment > query.end)
                    or (
                        query.q
                        and query.q.casefold()
                        not in (review["data"]["name"] + review["reason"]).casefold()
                    )
                ):
                    continue
                items.append(
                    {
                        **review,
                        "publicationId": record["id"],
                        "applicationId": record["applicationId"],
                    }
                )
        items.sort(key=lambda item: item["reviewedAt"], reverse=True)
        reviewers = {
            review["reviewerId"]: {
                "id": review["reviewerId"],
                "name": review.get("reviewerName", review["reviewerId"]),
            }
            for record in records
            for review in record["reviews"]
            if review.get("reviewerId")
        }
        return {
            **paginate(items, query.page, query.pageSize),
            "reviewers": list(reviewers.values()),
        }
