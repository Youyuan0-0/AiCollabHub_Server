"""数据集管理业务：组织隔离、审核流转及原子保存的审核历史。"""

from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException

from app.modules.applications.identity import Actor, can_manage
from app.modules.common.schemas import PageQuery
from app.modules.datasets.admin_schemas import (
    DATASET_STATUSES,
    DatasetAdminQuery,
    DatasetAuditQuery,
    DatasetListing,
    DatasetReviewQuery,
    DatasetVisibility,
    ReviewDataset,
)
from app.modules.datasets.schemas import DatasetInput, validate_submission
from app.modules.datasets.service import list_resources
from app.modules.datasets.workspace import DatasetWorkspace, timestamp


def require_dataset_admin(actor: Actor) -> None:
    """管理入口只允许系统或单位管理员，不接受前端声明的角色。"""
    if actor.role not in ("admin", "org_admin"):
        raise HTTPException(403, "当前账号没有数据集管理权限")


def paginate(items: list[dict], query: PageQuery) -> dict:
    """按已校验的页码返回切片及总数，超出范围返回空页。"""
    offset = (query.page - 1) * query.page_size
    return {
        "items": items[offset : offset + query.page_size],
        "total": len(items),
        "page": query.page,
        "pageSize": query.page_size,
    }


def matches_keyword(record: dict, keyword: str) -> bool:
    """搜索申请标识及资料；审计调用方传入历史快照，避免匹配后来的修改。"""
    data = record["data"]
    return (
        keyword.strip().casefold()
        in " ".join(
            [
                record.get("id", ""),
                record.get("datasetId", ""),
                data.get("name", ""),
                data.get("summary", ""),
                data.get("owner", ""),
                *data.get("tags", []),
            ]
        ).casefold()
    )


class DatasetAdminService:
    """复用数据集仓储；审核与记录在同一次 CAS 中提交，不影响下载收藏统计。"""

    def __init__(self, workspace: DatasetWorkspace) -> None:
        self.workspace = workspace

    def catalog(self, query: DatasetAdminQuery, actor: Actor) -> dict:
        """返回含上下架状态和操作权限的目录，状态过滤先于分页及总数计算。"""
        require_dataset_admin(actor)
        resources = self.workspace.resources(actor, include_unlisted=True)
        for resource in resources:
            resource["canManageListing"] = can_manage(actor, resource)
        return list_resources(
            query,
            [
                item
                for item in resources
                if not query.listing_status or item["listing"]["status"] == query.listing_status
            ],
        )

    def listing(self, dataset_id: str, payload: DatasetListing, actor: Actor) -> dict:
        """校验可信归属并原子保存上下架；保留发布快照、开放范围和用户活动。"""
        require_dataset_admin(actor)
        resource = self.workspace.resource(dataset_id, include_unlisted=True)
        if not can_manage(actor, resource):
            raise HTTPException(403, "只能上下架有管理权限的数据集")
        # 仅既有目录资源可调整状态，未审核或待同步申请不能通过此接口直接上架。
        value = self.workspace.repository.save(
            "listing:" + dataset_id,
            {
                **payload.model_dump(),
                "datasetId": dataset_id,
                "updatedAt": timestamp(),
                "updatedBy": actor.id,
            },
            payload.revision or None,
        )
        return {key: value[key] for key in ("status", "revision")}

    def visibility(self, dataset_id: str, payload: DatasetVisibility, actor: Actor) -> dict:
        """仅允许有资源管理权的身份按修订号保存范围，数据库与内存均原子防覆盖。"""
        require_dataset_admin(actor)
        resource = self.workspace.resource(dataset_id, include_unlisted=True)
        # 样例缺少可信组织编号时仅系统管理员可改，不能用 owner 显示名称推断授权。
        if not can_manage(actor, resource):
            raise HTTPException(403, "只能配置有管理权限的数据集开放范围")
        value = self.workspace.repository.save(
            "visibility:" + dataset_id,
            {**payload.model_dump(), "datasetId": dataset_id},
            payload.revision or None,
        )
        return {key: value[key] for key in ("scope", "targets", "revision")}

    def publications(self, actor: Actor) -> list[dict]:
        """返回管理范围内的申请；组织缺失不能获得单位管理权限。"""
        require_dataset_admin(actor)
        return [
            record
            for record in self.workspace.repository.list("publication:")
            if can_manage(actor, record)
        ]

    def publication(self, publication_id: str, actor: Actor) -> dict:
        """读取授权范围内的详情；跨组织访问与不存在统一返回 404。"""
        require_dataset_admin(actor)
        record = self.workspace.repository.get("publication:" + publication_id)
        if record is None or not can_manage(actor, record):
            raise HTTPException(404, "发布申请不存在或无权查看")
        return record

    def queue(self, query: DatasetReviewQuery, actor: Actor) -> dict:
        """先按权限、关键词和日期过滤，再汇总各状态数量并分页。"""
        records = []
        for record in self.publications(actor):
            submitted = (record.get("submittedAt") or "")[:10]
            if not matches_keyword(record, query.q):
                continue
            if query.start and submitted < query.start.isoformat():
                continue
            if query.end and (not submitted or submitted > query.end.isoformat()):
                continue
            records.append(record)
        counts = {
            status: sum(item["status"] == status for item in records) for status in DATASET_STATUSES
        }
        selected = [item for item in records if not query.status or item["status"] == query.status]
        key = "submittedAt" if query.sort == "submitted" else "updatedAt"
        selected.sort(key=lambda item: (item.get(key) or "", item["id"]), reverse=True)
        return {**paginate(selected, query), "counts": counts}

    def review(self, publication_id: str, payload: ReviewDataset, actor: Actor) -> dict:
        """仅审核当前待审修订；通过后由发布通道准备快照，与审核历史一并原子提交。"""
        record = self.publication(publication_id, actor)
        if record["revision"] != payload.revision or record["status"] != "pending":
            raise HTTPException(409, "申请状态或版本已更新，请刷新资料后再审核")
        if payload.decision == "approved":
            validate_submission(DatasetInput(**record["data"]))
        now = timestamp()
        review = {
            "id": "dsr-" + uuid4().hex,
            "decision": payload.decision,
            "reason": payload.reason,
            "reviewerId": actor.id,
            "reviewerName": actor.name,
            "reviewedAt": now,
            "submittedAt": record.get("submittedAt"),
            "revision": record["revision"],
            "scores": payload.scores.model_dump(by_alias=True) if payload.scores else None,
            "data": deepcopy(record["data"]),
        }
        record.update(
            status=payload.decision,
            syncStatus="pending" if payload.decision == "approved" else "not_ready",
            reviewComment=payload.reason,
            updatedAt=now,
            reviews=[*record.get("reviews", []), review],
        )
        if payload.decision == "approved":
            record.update(self.workspace.publisher.prepare(record))
        # 状态、上架和审核快照一次性保存；并发审核只有一个成功，不自动重试旧结论。
        return self.workspace.repository.save(
            "publication:" + publication_id, record, payload.revision
        )

    def publish(self, publication_id: str, revision: int, actor: Actor) -> dict:
        """补发历史已通过申请；检查管理权限和修订，已上架时幂等返回且不重复审核。"""
        record = self.publication(publication_id, actor)
        if record["status"] != "approved" or record["revision"] != revision:
            raise HTTPException(409, "申请状态或版本已更新，请刷新资料后再发布")
        if record.get("publishedResource"):
            return record
        record["updatedAt"] = timestamp()
        prepared = self.workspace.publisher.prepare(record)
        if not prepared.get("publishedResource"):
            raise HTTPException(503, "当前发布通道尚未就绪，请稍后重试")
        record.update(prepared)
        return self.workspace.repository.save("publication:" + publication_id, record, revision)

    def audit(self, query: DatasetAuditQuery, actor: Actor) -> dict:
        """返回授权范围内的审核快照及审核人选项，支持资料和审核意见检索。"""
        records = self.publications(actor)
        # 选项来自完整的可管理历史，不随分页或筛选消失，也不暴露其他组织的审核人。
        reviewers = {
            review["reviewerId"]: {
                "id": review["reviewerId"],
                "name": review.get("reviewerName") or review["reviewerId"],
            }
            for record in records
            for review in record.get("reviews", [])
            if review.get("reviewerId")
        }
        reviewer = query.reviewer.strip()
        keyword = query.q.strip().casefold()
        entries = []
        for record in records:
            for review in record.get("reviews", []):
                entry = {**review, "publicationId": record["id"], "datasetId": record["datasetId"]}
                if query.decision and entry["decision"] != query.decision:
                    continue
                if not matches_keyword({**entry, "id": record["id"]}, query.q) and (
                    keyword not in entry.get("reason", "").casefold()
                ):
                    continue
                # 下拉选中的可信编号精确匹配；旧链接中的姓名或编号片段仍支持模糊查询。
                if reviewer in reviewers and entry.get("reviewerId") != reviewer:
                    continue
                if (
                    reviewer
                    and reviewer not in reviewers
                    and not any(
                        reviewer.casefold() in str(entry.get(key, "")).casefold()
                        for key in ("reviewerId", "reviewerName")
                    )
                ):
                    continue
                reviewed = entry["reviewedAt"][:10]
                if query.start and reviewed < query.start.isoformat():
                    continue
                if query.end and reviewed > query.end.isoformat():
                    continue
                entries.append(entry)
        entries.sort(key=lambda item: (item["reviewedAt"], item["id"]), reverse=True)
        return {**paginate(entries, query), "reviewers": list(reviewers.values())}
