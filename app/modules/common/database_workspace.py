"""模型与目录型知识库的持久业务编排，复用正式发布和共用互动仓储。"""

from copy import deepcopy
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from app.modules.common.identity import Actor, can_manage, require_admin, visible
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_workspace import ResourceReview, ResourceVisibility
from app.modules.common.service import timestamp


class DatabaseResourceWorkspace:
    """将现有资源页面流程接到新库，字段验证继续由各模块负责。"""

    def __init__(self, repository: ResourceDatabase) -> None:
        self.repository = repository
        self.kind = repository.kind
        self.lock = RLock()

    @staticmethod
    def check_resource(item: dict | None, actor: Actor, admin: bool) -> dict:
        """管理员和用户分别检查管理权限、启用状态及可见用户范围。"""
        if item is None or not (
            can_manage(actor, item)
            if admin
            else item["listingStatus"] == "listed" and visible(actor, item, item["visibility"])
        ):
            raise HTTPException(404, "资源不存在或无权查看")
        return item

    @staticmethod
    def check_publication(item: dict | None, actor: Actor, admin: bool) -> dict:
        """申请只能由实际申请人或有管理权限的身份读取。"""
        if item is None or not (
            can_manage(actor, item) if admin else item["createdBy"] == actor.id
        ):
            raise HTTPException(404, "申请不存在或无权查看")
        return item

    def find_resource(self, resource_id: str) -> dict | None:
        """仅供已经验证申请归属的模块校验当前版本。"""
        with self.repository.transaction(is_readonly=True) as session:
            return session.get_resource(resource_id)

    def resource(self, identifier: str, actor: Actor, admin: bool = False) -> dict:
        """读取授权范围内的正式详情，并附上本人的收藏状态。"""
        with self.repository.transaction(is_readonly=True) as session:
            item = self.check_resource(session.get_resource(identifier), actor, admin)
            if not admin:
                interaction = session.entry("interactions", f"{actor.id}:{identifier}") or {}
                item["isFavorite"] = bool(interaction.get("favorite"))
            return item

    def catalog(self, actor: Actor, admin: bool = False) -> list[dict]:
        """先进行启用状态和权限过滤，再由页面接口执行搜索与分页。"""
        if admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            favorites = {
                item[session.resource_key]
                for item in session.interaction_entries(subject=actor.id)
                if item["actorId"] == actor.id and item["favorite"]
            }
            return [
                {**item, "isFavorite": item["id"] in favorites}
                for item in session.list_resources()
                if (
                    can_manage(actor, item)
                    if admin
                    else item["listingStatus"] == "listed"
                    and visible(actor, item, item["visibility"])
                )
            ]

    def publication(self, identifier: str, actor: Actor, admin: bool = False) -> dict:
        """按申请编号读取当前资料和不可变审核历史。"""
        with self.repository.transaction(is_readonly=True) as session:
            return self.check_publication(session.get_publication(identifier), actor, admin)

    def publications(self, actor: Actor, admin: bool = False) -> list[dict]:
        """查询当前身份可访问的申请。"""
        if admin:
            require_admin(actor)
        with self.repository.transaction(is_readonly=True) as session:
            return [
                item
                for item in session.list_publications()
                if (can_manage(actor, item) if admin else item["createdBy"] == actor.id)
            ]

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
        """创建或更新发布申请，只保存申请资料，不直接改写正式目录。

        identifier 为空时新建申请；更新时 revision 必须等于页面读取的当前修订号。
        submit 决定保存草稿还是进入待审，待审和已通过申请不能原地编辑。
        为已有 resource_id 发布新版本时，记录当前 versionId 作为基线，审核发布时
        再次比对，防止较旧申请覆盖已经发布的新版本。
        """
        if actor.role not in {"publisher", "admin", "org_admin"}:
            raise HTTPException(403, "当前身份不能发布资源")
        with self.repository.transaction() as session:
            previous = (
                self.check_publication(session.get_publication(identifier), actor, False)
                if identifier
                else None
            )
            if previous and (
                previous["revision"] != revision or previous["status"] in {"pending", "approved"}
            ):
                raise HTTPException(409, "申请已更新或不可编辑，请刷新")
            now = timestamp()
            record = {
                **(previous or {}),
                "id": identifier or str(uuid4()),
                "resourceId": previous["resourceId"] if previous else resource_id or str(uuid4()),
                "createdBy": actor.id,
                "createdByName": actor.name,
                "organizationId": actor.organization_id,
                "createdAt": previous["createdAt"] if previous else now,
                "updatedAt": now,
                "submittedAt": now if submit else None,
                "revision": (revision or 0) + 1,
                "status": "pending" if submit else "draft",
                "data": deepcopy(data),
                "reviews": previous["reviews"] if previous else [],
                "kind": self.kind,
            }
            if resource_id and previous is None:
                source = session.get_resource(resource_id)
                if source is None or not (
                    source["createdBy"] == actor.id or can_manage(actor, source)
                ):
                    raise HTTPException(404, "资源不存在或无权发布版本")
                record["baseVersionId"] = source["versionId"]
            session.put_publication(record)
            return record

    def review(
        self, identifier: str, payload: ResourceReview, actor: Actor, reports: list[dict]
    ) -> dict:
        """校验本轮申请修订并保存结论，由仓储原子完成历史归档和版本发布。

        reviews 中保留本轮资料的深拷贝，后续退回编辑不会改变旧审核记录。
        通过审核只形成正式版本；首次发布仍禁用，需管理员另行启用才进入用户目录。
        """
        require_admin(actor)
        with self.repository.transaction() as session:
            record = self.check_publication(session.get_publication(identifier), actor, True)
            if record["status"] != "pending" or record["revision"] != payload.revision:
                raise HTTPException(409, "申请状态或修订已变化")
            record["reviews"].append(
                {
                    "id": str(uuid4()),
                    "reviewerId": actor.id,
                    "reviewerName": actor.name,
                    "reviewedAt": timestamp(),
                    "decision": payload.decision,
                    "reason": payload.reason,
                    "scores": payload.scores.model_dump(by_alias=True) if payload.scores else None,
                    "reportFiles": reports,
                    "data": deepcopy(record["data"]),
                }
            )
            record.update(status=payload.decision, revision=record["revision"] + 1)
            session.put_publication(record)
            return record

    def update_metadata(self, identifier: str, changes: dict, revision: int, actor: Actor) -> dict:
        """冻结后的送审资料不能由管理员原地更改，退回后由申请人重新提交。"""
        require_admin(actor)
        self.publication(identifier, actor, True)
        raise HTTPException(409, "送审资料已冻结，请退回申请人修改后重新提交")

    def set_visibility(self, identifier: str, payload: ResourceVisibility, actor: Actor) -> dict:
        """按当前规则修订保存已确认的指定用户可见范围。"""
        require_admin(actor)
        with self.repository.transaction() as session:
            self.check_resource(session.get_resource(identifier), actor, True)
            current = session.get_visibility(identifier)
            if current["revision"] != payload.revision:
                raise HTTPException(409, "权限配置已变化，请刷新")
            result = {**payload.model_dump(), "revision": current["revision"] + 1}
            session.put_visibility(identifier, result)
            return result

    def set_listing(self, identifier: str, status: str, revision: int, actor: Actor) -> dict:
        """独立启用或禁用正式资源，保留审核与版本历史。"""
        require_admin(actor)
        with self.repository.transaction() as session:
            item = self.check_resource(session.get_resource(identifier), actor, True)
            if item["revision"] != revision:
                raise HTTPException(409, "资源已更新，请刷新")
            item["listingStatus"] = status
            session.save_resource_state(item)
            return session.get_resource(identifier)

    def interact(
        self,
        identifier: str,
        actor: Actor,
        *,
        favorite: bool | None = None,
        download: bool = False,
        download_request_id: str | None = None,
    ) -> dict:
        """保存本人收藏或下载请求，统计从共用明细重新聚合。"""
        with self.repository.transaction() as session:
            item = self.check_resource(session.get_resource(identifier), actor, False)
            key = f"{actor.id}:{identifier}"
            if favorite is not None:
                session.put_entry(
                    "interactions",
                    key,
                    {"actorId": actor.id, "resourceId": identifier, "favorite": favorite},
                )
            if download:
                session.put_entry(
                    "downloads",
                    key + ":" + (download_request_id or str(uuid4())),
                    {
                        "actorId": actor.id,
                        "resourceId": identifier,
                        "versionId": item["versionId"],
                    },
                )
            result = session.get_resource(identifier)
            result["isFavorite"] = bool((session.entry("interactions", key) or {}).get("favorite"))
            return result
