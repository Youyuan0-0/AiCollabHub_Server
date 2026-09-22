"""资源模拟流程的公共底层；业务字段由调用模块校验，不接触数据库。"""

from copy import deepcopy
from threading import RLock
from typing import Literal, Self
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, model_validator

from app.modules.common.access_schemas import ScopeTargets, StrictPayload
from app.modules.common.identity import Actor, can_manage, require_admin, visible
from app.modules.common.review_schemas import ReviewQualityScores
from app.modules.common.service import timestamp


class ResourceVisibility(ScopeTargets):
    """新资源只配置集团、单位或项目范围，修订号防止旧页面覆盖。"""

    scope: Literal["all", "members", "organizations", "projects"] = "all"
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def require_targets(self) -> Self:
        """非全集团范围必须指定授权目标。"""
        if self.scope != "all" and not self.targets:
            raise ValueError("请选择开放目标")
        if self.scope == "all":
            self.targets = []
        return self


class ResourceReview(StrictPayload):
    """审核通用结论和七项评分，业务完整性由资源模块再次检查。"""

    decision: Literal["approved", "rejected", "needs_changes"]
    revision: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)
    scores: ReviewQualityScores | None = None
    report_file_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_decision(self) -> Self:
        """通过需完整评分，退回需原因，附件不能重复。"""
        if self.decision == "approved" and self.scores is None:
            raise ValueError("请完成七项质量评分")
        if self.decision != "approved" and not self.reason:
            raise ValueError("请填写退回原因")
        if len(set(self.report_file_ids)) != len(self.report_file_ids):
            raise ValueError("附件编号重复")
        return self


class ResourceWorkspace:
    """实例隔离的发布、修订、审核、授权和互动状态，真实仓储从此处替换。"""

    def __init__(self, kind: str, seeds: list[dict] | None = None) -> None:
        self.kind = kind
        self.lock = RLock()
        self.records: dict[str, dict] = {}
        self.resources: dict[str, dict] = {}
        self.activities: dict[tuple[str, str], dict] = {}
        for seed in seeds or []:
            item = deepcopy(seed)
            item.update(
                revision=1,
                visibility={"scope": "all", "targets": [], "revision": 1},
                listingStatus="listed",
                createdAt=item.get("createdAt", item.get("updatedAt", "2026-09-01T00:00:00Z")),
                versions=item.get("versions", []),
            )
            self.resources[item["id"]] = item

    def resource(self, identifier: str, actor: Actor, admin: bool = False) -> dict:
        """读取资源前校验管理或开放范围，不通过编号泄露隐藏资源。"""
        with self.lock:
            item = self.resources.get(identifier)
            if not item:
                raise HTTPException(404, "资源不存在")
            allowed = (
                can_manage(actor, item)
                if admin
                else (
                    item.get("createdBy") == actor.id
                    or (
                        item["listingStatus"] == "listed"
                        and visible(actor, item, item["visibility"])
                    )
                )
            )
            if not allowed:
                raise HTTPException(404, "资源不存在或无权查看")
            result = deepcopy(item)
            if not admin:
                result["isFavorite"] = self.activities.get((actor.id, identifier), {}).get(
                    "favorite", False
                )
            return result

    def find_resource(self, resource_id: str) -> dict | None:
        """向模块提供当前版本副本，避免调用方直接依赖内存字典。"""
        with self.lock:
            return deepcopy(self.resources.get(resource_id))

    def catalog(self, actor: Actor, admin: bool = False) -> list[dict]:
        """按权限过滤后返回副本，分页和统计均在过滤之后进行。"""
        if admin:
            require_admin(actor)
        with self.lock:
            identifiers = list(self.resources)
        result = []
        for identifier in identifiers:
            try:
                result.append(self.resource(identifier, actor, admin))
            except HTTPException as error:
                if error.status_code != 404:
                    raise
        return result

    def publication(self, identifier: str, actor: Actor, admin: bool = False) -> dict:
        """申请只向发布者或所属管理人员开放。"""
        with self.lock:
            item = self.records.get(identifier)
            if not item or not (
                can_manage(actor, item) if admin else item["createdBy"] == actor.id
            ):
                raise HTTPException(404, "申请不存在或无权查看")
            return deepcopy(item)

    def publications(self, actor: Actor, admin: bool = False) -> list[dict]:
        """查询当前身份可见申请，保留真实审核状态。"""
        if admin:
            require_admin(actor)
        with self.lock:
            return deepcopy(
                [
                    item
                    for item in self.records.values()
                    if (can_manage(actor, item) if admin else item["createdBy"] == actor.id)
                ]
            )

    def save(
        self,
        data: dict,
        actor: Actor,
        *,
        submit: bool = False,
        identifier: str | None = None,
        revision: int | None = None,
        resource_id: str | None = None,
    ) -> dict:
        """保存已由业务层校验的草稿，提交锁定本次修订，历史审核不可覆盖。"""
        if actor.role not in ("publisher", "admin", "org_admin"):
            raise HTTPException(403, "当前身份不能发布资源")
        with self.lock:
            old = self.publication(identifier, actor) if identifier else None
            if old and (old["revision"] != revision or old["status"] in ("pending", "approved")):
                raise HTTPException(409, "申请已更新或不可编辑，请刷新")
            now = timestamp()
            item = {
                "id": identifier or f"{self.kind}-pub-{uuid4().hex}",
                "resourceId": old["resourceId"]
                if old
                else resource_id or f"{self.kind}-{uuid4().hex}",
                "kind": self.kind,
                "createdBy": actor.id,
                "organizationId": actor.organization_id,
                "createdAt": old["createdAt"] if old else now,
                "updatedAt": now,
                "submittedAt": now if submit else None,
                "revision": old["revision"] + 1 if old else 1,
                "status": "pending" if submit else "draft",
                "data": deepcopy(data),
                "reviews": old["reviews"] if old else [],
            }
            self.records[item["id"]] = item
            return deepcopy(item)

    def review(
        self, identifier: str, payload: ResourceReview, actor: Actor, reports: list[dict]
    ) -> dict:
        """在锁内保存审核快照并上架，版本更新保留原有授权与互动统计。"""
        require_admin(actor)
        with self.lock:
            item = self.publication(identifier, actor, True)
            if item["status"] != "pending" or item["revision"] != payload.revision:
                raise HTTPException(409, "申请状态或修订已变化")
            now = timestamp()
            review = {
                "id": uuid4().hex,
                "reviewerId": actor.id,
                "reviewerName": actor.name,
                "reviewedAt": now,
                "decision": payload.decision,
                "reason": payload.reason,
                "scores": payload.scores.model_dump(by_alias=True) if payload.scores else None,
                "data": deepcopy(item["data"]),
                "reportFiles": reports,
            }
            item.update(status=payload.decision, updatedAt=now, revision=item["revision"] + 1)
            item["reviews"].append(review)
            if item["resourceId"] in self.resources:
                self.resources[item["resourceId"]]["publicationStatus"] = payload.decision
            if payload.decision == "approved":
                previous = self.resources.get(item["resourceId"], {})
                resource = {
                    "downloadCount": 0,
                    "favoriteCount": 0,
                    "usage": 0,
                    **deepcopy(previous),
                    **deepcopy(item["data"]),
                    "id": item["resourceId"],
                    "kind": self.kind,
                    "createdBy": item["createdBy"],
                    "organizationId": item["organizationId"],
                    "createdAt": previous.get("createdAt", item["createdAt"]),
                    "updatedAt": now,
                    "revision": previous.get("revision", 0) + 1,
                    "listingStatus": "listed"
                    if not previous.get("approvedAt")
                    else previous.get("listingStatus", "listed"),
                    "approvedAt": now,
                    "publicationStatus": "approved",
                    "visibility": previous.get(
                        "visibility", {"scope": "all", "targets": [], "revision": 1}
                    ),
                    "rating": round(sum(payload.scores.model_dump().values()) / 7, 2)
                    if payload.scores
                    else previous.get("rating"),
                    "versions": [
                        *previous.get("versions", []),
                        {
                            "version": item["data"].get(
                                "version", f"v{len(previous.get('versions', [])) + 1}.0"
                            ),
                            "releasedAt": now,
                            "note": payload.reason,
                            "data": deepcopy(item["data"]),
                        },
                    ],
                }
                self.resources[resource["id"]] = resource
            self.records[identifier] = item
            return deepcopy(item)

    def update_metadata(self, identifier: str, changes: dict, revision: int, actor: Actor) -> dict:
        """在同一修订内维护管理员资料，业务模块先校验允许修改的字段。"""
        require_admin(actor)
        with self.lock:
            record = self.publication(identifier, actor, True)
            if record["revision"] != revision or record["status"] != "pending":
                raise HTTPException(409, "申请已变化，请刷新")
            record["data"].update(deepcopy(changes))
            record["revision"] += 1
            record["updatedAt"] = timestamp()
            self.records[identifier] = record
            return deepcopy(record)

    def set_visibility(self, identifier: str, payload: ResourceVisibility, actor: Actor) -> dict:
        """按授权修订保存范围，不改其他资源字段。"""
        require_admin(actor)
        with self.lock:
            item = self.resource(identifier, actor, True)
            if item["visibility"]["revision"] != payload.revision:
                raise HTTPException(409, "权限配置已变化，请刷新")
            item["visibility"] = {**payload.model_dump(), "revision": payload.revision + 1}
            self.resources[identifier] = item
            return deepcopy(item["visibility"])

    def interact(
        self, identifier: str, actor: Actor, *, favorite: bool | None = None, download: bool = False
    ) -> dict:
        """服务端统计收藏和下载，重复收藏不重复计数。"""
        with self.lock:
            item = self.resource(identifier, actor)
            item.pop("isFavorite", None)
            activity = self.activities.setdefault((actor.id, identifier), {"favorite": False})
            if favorite is not None:
                item["favoriteCount"] = (
                    item.get("favoriteCount", 0) + int(favorite) - int(activity["favorite"])
                )
                activity["favorite"] = favorite
            if download:
                item["downloadCount"] = item.get("downloadCount", 0) + 1
                item["lastDownloadedAt"] = timestamp()
            self.resources[identifier] = item
            return {**deepcopy(item), "isFavorite": activity["favorite"]}
