"""受本地应用权限约束的外部发布流程；外部请求在本地事务提交后执行。"""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import quote

from fastapi import HTTPException

from app.modules.applications.identity import can_edit, can_manage, require_admin
from app.modules.applications.market_contract import (
    LISTING_FIELDS,
    REVIEW_FIELDS,
    HiMarketClient,
    MarketFault,
    external_fields,
    select_fields,
)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def now():
    return datetime.now(UTC).isoformat()


def listing_fingerprint(listing):
    return digest(
        {
            key: listing.get(key)
            for key in (
                "resourceId",
                "defaultVersionId",
                "title",
                "summary",
                "description",
                "visibility",
                "categoryId",
            )
        }
    )


class MarketWorkflows:
    def __init__(self, service, settings, transport=None):
        self.service = service
        self.settings = settings
        self.client = HiMarketClient(settings, transport)

    def status(self, actor, *, admin=False):
        if admin:
            require_admin(actor)
        if self.settings.applications_market != "himarket":
            return {
                "enabled": False,
                "message": "外部发布服务尚未启用",
                "applications": [],
                "decisions": [],
            }
        config = self.client.configuration()
        applications = []
        # 仅对已在本平台登记的应用开放外部写入，不凭浏览器传来的资源编号代理管理权限。
        with self.service.repository.transaction(readonly=True) as session:
            for resource in session.list_resources():
                binding = config.bindings.get(resource["id"])
                if binding and self.allowed(actor, resource, binding, admin=admin):
                    applications.append(
                        {
                            "id": resource["id"],
                            "name": resource["name"],
                            "resourceId": binding.resource_id,
                        }
                    )
        return {
            "enabled": True,
            "message": "已启用外部发布服务，操作结果以外部查询为准",
            "applications": applications,
            "decisions": list(config.decision_status),
            "approvedStatus": config.decision_status.get("approved"),
        }

    @staticmethod
    def allowed(actor, resource, binding, *, admin):
        ownership_matches = (
            resource.get("createdBy") == binding.owner_subject
            and resource.get("organizationId") == binding.organization_id
        )
        return ownership_matches and (
            can_manage(actor, resource)
            if admin
            else can_edit(actor, resource) and actor.id == binding.owner_subject
        )

    def context(self, application_id, actor, *, admin=False):
        if admin:
            require_admin(actor)
        if self.settings.applications_market != "himarket":
            raise HTTPException(503, "外部发布服务尚未启用")
        config = self.client.configuration()
        binding = config.bindings.get(application_id)
        with self.service.repository.transaction(readonly=True) as session:
            resource = session.get_resource(application_id)
        if not binding or not resource or not self.allowed(actor, resource, binding, admin=admin):
            raise HTTPException(404, "未找到有权限操作的外部应用")
        return config, binding

    def records(self, application_id, config, session=None):
        if session is None:
            with self.service.repository.transaction(readonly=True) as session:
                return self.records(application_id, config, session)
        return sorted(
            (
                item
                for item in session.entries("market_operations")
                if item["applicationId"] == application_id and item["profile"] == config.profile
            ),
            key=lambda item: item["createdAt"],
        )

    def listing_id(self, binding, records):
        ids = {
            item["result"]["listingId"]
            for item in records
            if item.get("result", {}).get("listingId")
        }
        if binding.listing_id:
            ids.add(binding.listing_id)
        if len(ids) > 1:
            raise HTTPException(409, "外部条目对应关系发生冲突，请核对配置")
        return next(iter(ids), None)

    def checked_listing(self, config, binding, listing_id):
        item = self.client.listing(config, listing_id)
        if (
            item.get("listingId") != listing_id
            or item.get("resourceId") != binding.resource_id
            or binding.portal_id
            and item.get("portalId") != binding.portal_id
        ):
            raise MarketFault("外部条目与当前应用对应关系不一致")
        if item.get("publicationStatus") not in (
            "DRAFT",
            "IN_REVIEW",
            "PUBLISHED",
            "UNPUBLISHED",
            "REJECTED",
        ):
            raise MarketFault("外部条目状态未识别，请先确认状态定义")
        return item

    def detail(self, application_id, actor, *, admin=False):
        config, binding = self.context(application_id, actor, admin=admin)
        records = self.records(application_id, config)
        listing_id = self.listing_id(binding, records)
        read_error = ""
        try:
            listing = self.checked_listing(config, binding, listing_id) if listing_id else None
        except HTTPException as error:
            listing, read_error = None, str(error.detail)
        # 仅展示本应用已登记的提交记录，不向普通账号开放管理面的全量审核列表。
        submissions = []
        seen = set()
        for record in reversed(records):
            submission_id = record.get("result", {}).get("submissionId")
            if submission_id and submission_id not in seen:
                try:
                    item = self.client.review(config, submission_id)
                    if item.get("targetId") != listing_id:
                        raise MarketFault("外部审核记录与当前条目不匹配")
                    submissions.append(item)
                except HTTPException as error:
                    read_error = str(error.detail)
                seen.add(submission_id)
            if len(submissions) == 10:
                break
        return {
            "applicationId": application_id,
            "resourceId": binding.resource_id,
            "listing": listing,
            "submissions": submissions,
            "readError": read_error,
            "operations": [self.public_record(item) for item in reversed(records[-30:])],
        }

    @staticmethod
    def public_record(record):
        return select_fields(record, ("id", "action", "status", "message", "createdAt", "result"))

    def versions(self, application_id, actor, page, size):
        config, binding = self.context(application_id, actor)
        return self.client.versions(config, binding, actor.id, page, size)

    def execute(self, application_id, actor, payload, *, admin=False):
        config, binding = self.context(application_id, actor, admin=admin)
        if (payload.action in ("review_decision", "publish")) != admin:
            raise HTTPException(403, "请使用对应的发布或管理入口")
        fingerprint = digest(
            {
                "payload": payload.model_dump(by_alias=True),
                "binding": binding.model_dump(),
                "profile": config.profile,
            }
        )
        key = digest([config.profile, actor.id, application_id, payload.request_id])
        # 预留请求记录后提交事务，超时或进程中断不会被当成“从未请求过”。
        with self.service.repository.transaction() as session:
            session.entry("market_lock", application_id)
            previous = session.entry("market_operations", key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise HTTPException(409, "请求编号已用于其他内容，请重新核对")
                return self.public_record(previous)
            records = self.records(application_id, config, session)
            if any(item["status"] in ("running", "unknown") for item in records):
                raise HTTPException(409, "该应用还有结果未确认的操作，请先查询结果")
            listing_id = self.listing_id(binding, records)
            if payload.action != "save_draft" and not listing_id:
                raise HTTPException(409, "请先保存外部草稿，取得市场条目编号")
            if payload.action == "save_draft" and not payload.draft:
                raise HTTPException(422, "请填写草稿资料")
            if payload.action == "review_decision":
                if not payload.submission_id or not payload.decision:
                    raise HTTPException(422, "请选择审核记录和审核结论")
                if payload.decision not in config.decision_status:
                    raise HTTPException(503, "该审核结论尚未配置外部状态值，请向供应方确认")
                if payload.decision != "approved" and not payload.comment:
                    raise HTTPException(422, "驳回或补充材料时请填写原因")
                if payload.decision == "approved":
                    threshold = self.service.workflows.policy_in(session)["minimumQualityScore"]
                    if (
                        payload.scores is None
                        or min(payload.scores.model_dump().values()) < threshold
                    ):
                        raise HTTPException(422, "请完成完整性、可用性和安全性评分，并达到通过阈值")
                known = {item.get("result", {}).get("submissionId") for item in records}
                if payload.submission_id not in known:
                    raise HTTPException(404, "该审核记录未在当前应用中登记")
            record = {
                "id": key,
                "applicationId": application_id,
                "actorId": actor.id,
                "organizationId": binding.organization_id,
                "profile": config.profile,
                "requestId": payload.request_id,
                "fingerprint": fingerprint,
                "action": payload.action,
                "status": "running",
                "createdAt": now(),
                "message": "正在请求外部服务",
                "result": {},
                "completedAt": None,
                "listingId": listing_id,
                "resourceId": binding.resource_id,
                "submissionId": payload.submission_id,
                "expectedDecision": config.decision_status.get(payload.decision),
                "scores": payload.scores.model_dump() if payload.scores else None,
                "comment": payload.comment,
                "expectedDraftHash": digest(
                    payload.draft.model_dump(by_alias=True, exclude_none=True)
                )
                if payload.draft
                else None,
                "draftKeys": list(payload.draft.model_dump(by_alias=True, exclude_none=True))
                if payload.draft
                else [],
            }
            session.put_entry("market_operations", key, record)
        attempted = False
        try:
            current = self.checked_listing(config, binding, listing_id) if listing_id else None
            submitted = payload.draft.model_dump(by_alias=True) if payload.draft else current or {}
            with self.service.repository.transaction(readonly=True) as session:
                local = session.get_resource(application_id)
                level = local.get("securityLevel", "internal")
                level = {"内部": "internal", "公开": "public", "机密": "confidential"}.get(
                    level, level
                )
                self.service.workflows.validate_security(
                    session,
                    {
                        "name": submitted.get("title", ""),
                        "summary": submitted.get("summary", ""),
                        "descriptionMarkdown": submitted.get("description", ""),
                    },
                    level,
                )
                if submitted.get("visibility") == "PUBLIC" and level != "public":
                    raise HTTPException(422, "内部应用不能直接设置为外部公开，请先核对密级")
            if payload.action == "save_draft":
                if current and current["publicationStatus"] not in (
                    "DRAFT",
                    "REJECTED",
                    "UNPUBLISHED",
                ):
                    raise HTTPException(409, "当前外部状态不能修改草稿，请先处理审核或发布状态")
                self.client.version(config, binding, actor.id, payload.draft.default_version_id)
                body = {
                    **payload.draft.model_dump(by_alias=True, exclude_none=True),
                    "resourceId": binding.resource_id,
                }
                if binding.portal_id:
                    body["portalId"] = binding.portal_id
                path, subject = "/me/listings", actor.id
            elif payload.action == "submit_review":
                if current["publicationStatus"] not in ("DRAFT", "REJECTED", "UNPUBLISHED"):
                    raise HTTPException(409, "当前外部状态不能重复提交审核")
                path = "/me/listings/" + quote(listing_id, safe="") + "/submit-review"
                body, subject = {}, actor.id
                record["submittedListingHash"] = listing_fingerprint(current)
            elif payload.action == "review_decision":
                submitted_records = [
                    item
                    for item in records
                    if item["action"] == "submit_review" and item["status"] == "succeeded"
                ]
                if (
                    not submitted_records
                    or submitted_records[-1]["result"].get("submissionId") != payload.submission_id
                    or submitted_records[-1].get("submittedListingHash")
                    != listing_fingerprint(current)
                ):
                    raise HTTPException(409, "待审版本或资料已变化，请重新核对提交内容")
                review = self.client.review(config, payload.submission_id)
                if review.get("targetId") != listing_id or review.get("status") != "PENDING":
                    raise HTTPException(409, "外部审核记录已变化，请刷新后处理")
                path = "/catalog/reviews/" + quote(payload.submission_id, safe="") + "/decision"
                body, subject = (
                    {"status": record["expectedDecision"], "comment": payload.comment},
                    None,
                )
            else:
                approval = config.decision_status.get("approved")
                if not approval:
                    raise HTTPException(503, "尚未确认外部审核通过状态，不能发布")
                submissions = [
                    item
                    for item in records
                    if item["action"] == "submit_review" and item["status"] == "succeeded"
                ]
                if not submissions:
                    raise HTTPException(409, "请先提交并完成外部审核")
                if submissions[-1].get("submittedListingHash") != listing_fingerprint(current):
                    raise HTTPException(409, "外部资料已变化，请重新提交当前版本审核")
                review = self.client.review(config, submissions[-1]["result"]["submissionId"])
                if review.get("targetId") != listing_id or review.get("status") != approval:
                    raise HTTPException(409, "外部审核尚未通过，不能发布")
                if current["publicationStatus"] == "PUBLISHED":
                    raise HTTPException(409, "外部条目已发布，请刷新查看")
                path, body, subject = (
                    "/catalog/listings/" + quote(listing_id, safe="") + "/publish",
                    None,
                    None,
                )
            # 凭证问题发生在实际请求之前，可以确认未写入。
            self.client.headers(config, subject=subject)
            self.store(record)
            attempted = True
            value = self.client.request(config, "POST", path, subject=subject, body=body)
            if payload.action in ("submit_review", "review_decision"):
                result = external_fields(value, REVIEW_FIELDS)
                if (
                    not result.get("submissionId")
                    or result.get("targetId") != listing_id
                    or payload.submission_id
                    and result["submissionId"] != payload.submission_id
                ):
                    raise MarketFault("外部返回的审核编号不匹配", uncertain=True)
                record["result"] = {**result, "listingId": listing_id}
            else:
                result = external_fields(value, LISTING_FIELDS)
                if (
                    not result.get("listingId")
                    or result.get("resourceId") != binding.resource_id
                    or listing_id
                    and result["listingId"] != listing_id
                ):
                    raise MarketFault("外部返回的条目编号不匹配", uncertain=True)
                record["listingId"] = result["listingId"]
                record["result"] = select_fields(
                    result, ("listingId", "resourceId", "defaultVersionId", "publicationStatus")
                )
            # 先保存回执编号；后续查询失败仍可恢复，不保存外部整份草稿资料。
            self.store(record)
            self.confirm(config, binding, record)
        except HTTPException as error:
            record["status"] = (
                "unknown"
                if attempted
                and (
                    bool(record["result"])
                    or not isinstance(error, MarketFault)
                    or not error.rejected
                )
                else "failed"
            )
            record["message"] = str(error.detail)
        self.finish(record)
        return self.public_record(record)

    def confirm(self, config, binding, record):
        listing_id = record["listingId"] or binding.listing_id
        if not listing_id:
            raise MarketFault("未取得条目编号，请核实外部结果后补充对应关系", uncertain=True)
        listing = self.checked_listing(config, binding, listing_id)
        action = record["action"]
        if action == "save_draft":
            actual = {key: listing.get(key) for key in record["draftKeys"]}
            if digest(actual) != record["expectedDraftHash"]:
                raise MarketFault("草稿内容尚未核对一致，请先确认外部保存结果", uncertain=True)
        elif action in ("submit_review", "review_decision"):
            submission_id = record["result"].get("submissionId") or record["submissionId"]
            if not submission_id:
                raise MarketFault("未取得审核编号，请先在外部核实本次提交", uncertain=True)
            review = self.client.review(config, submission_id)
            if review.get("targetId") != listing_id:
                raise MarketFault("审核与应用对应关系不一致", uncertain=True)
            if action == "review_decision" and review.get("status") != record["expectedDecision"]:
                raise MarketFault("审核结论尚未在外部确认，请稍后查询", uncertain=True)
            record["result"].update(review)
        elif listing["publicationStatus"] != "PUBLISHED":
            raise MarketFault("外部尚未确认正式发布，请稍后查询结果", uncertain=True)
        record["result"].update(
            select_fields(
                listing, ("listingId", "resourceId", "defaultVersionId", "publicationStatus")
            )
        )
        record.update(
            status="succeeded",
            message={
                "save_draft": "外部草稿已保存",
                "submit_review": "已提交外部审核",
                "review_decision": "外部审核结论已确认；是否发布请查看条目状态",
                "publish": "外部已确认正式发布",
            }[action],
        )

    def store(self, record):
        with self.service.repository.transaction() as session:
            previous = session.entry("market_operations", record["id"])
            if previous and previous["status"] in ("succeeded", "failed"):
                if previous != record:
                    raise HTTPException(409, "该外部操作已有最终结果，请刷新查看")
                return
            session.put_entry("market_operations", record["id"], record)

    def finish(self, record):
        record["completedAt"] = now() if record["status"] in ("succeeded", "failed") else None
        self.store(record)

    def reconcile(self, application_id, operation_id, actor, *, admin=False, recovery=None):
        config, binding = self.context(application_id, actor, admin=admin)
        with self.service.repository.transaction(readonly=True) as session:
            record = session.entry("market_operations", operation_id)
        if (
            not record
            or record["applicationId"] != application_id
            or record["profile"] != config.profile
            or record["resourceId"] != binding.resource_id
            or not admin
            and record["actorId"] != actor.id
        ):
            raise HTTPException(404, "操作记录不存在")
        if record["status"] not in ("running", "unknown"):
            return self.public_record(record)
        # 请求仍可能在当前进程执行；不与正在回写的操作竞争。
        if (
            record["status"] == "running"
            and (datetime.now(UTC) - datetime.fromisoformat(record["createdAt"])).total_seconds()
            < self.settings.applications_market_timeout * 5 + 10
        ):
            raise HTTPException(409, "外部请求正在处理中，请稍后查询")
        try:
            if recovery and recovery.external_submission_id:
                if record["action"] != "submit_review" or record["result"].get("submissionId"):
                    raise HTTPException(409, "该操作不需要补充审核回执编号")
                listing_id = record["listingId"] or binding.listing_id
                receipt = self.client.review(config, recovery.external_submission_id)
                try:
                    submitted_at = datetime.fromisoformat(receipt.get("submittedAt", ""))
                    timely = submitted_at >= datetime.fromisoformat(
                        record["createdAt"]
                    ) - timedelta(seconds=5)
                except (ValueError, TypeError):
                    timely = False
                if (
                    receipt.get("targetId") != listing_id
                    or receipt.get("submissionId") != recovery.external_submission_id
                    or not timely
                ):
                    raise HTTPException(409, "回执的应用或提交时间不匹配，请核对原请求")
                listing = self.checked_listing(config, binding, listing_id)
                if listing_fingerprint(listing) != record.get("submittedListingHash"):
                    raise HTTPException(409, "外部资料已变化，无法将回执归入本次提交")
                record["result"] = {**receipt, "listingId": listing_id}
                self.store(record)
            self.confirm(config, binding, record)
        except HTTPException as error:
            record.update(status="unknown", message=str(error.detail))
        self.finish(record)
        return self.public_record(record)
