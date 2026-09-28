"""知识库创建及文档审核；审核状态与索引状态分开存储。"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from app.modules.common.fixtures import paginate
from app.modules.common.identity import Actor, can_manage, require_admin
from app.modules.knowledge.documents import parse_document
from app.modules.knowledge.schemas import ResubmitInput, ReviewInput, ReviewQuery

if TYPE_CHECKING:
    from app.modules.knowledge.service import KnowledgeService


def review_state(item: dict) -> dict:
    """既有记录继续可用并明确标记历史数据；新建和新上传必须显式进入待审核。"""
    return item.get("review", {"status": "approved", "legacy": True, "reason": "历史记录"})


def is_approved(item: dict) -> bool:
    """判断业务审批是否通过，不把索引就绪当作审批通过。"""
    return review_state(item)["status"] == "approved"


def pending_review(actor: Actor) -> dict:
    """使用可信身份登记提交者，重新提交时清空上一轮结论。"""
    return {
        "status": "pending",
        "submittedAt": datetime.now(UTC).isoformat(),
        "submittedBy": actor.id,
        "submittedName": actor.name,
        "reviewedAt": None,
        "reviewer": "",
        "reason": "",
    }


class KnowledgeReviews:
    """复用知识库仓储及乐观锁，审核结论、历史和索引任务一并提交。"""

    def __init__(self, service: "KnowledgeService") -> None:
        self.service = service

    def queue(self, actor: Actor, query: ReviewQuery, *, audit: bool = False) -> dict:
        """返回当前管理员权限内的申请或审核事件，历史保留已删除文档的名称。"""
        require_admin(actor)
        rows = []
        for base in self.service.repository.list():
            if not can_manage(actor, base):
                continue
            if audit:
                rows.extend(
                    {**event, "baseId": base["id"], "baseName": base["name"]}
                    for event in base.get("reviewHistory", [])
                )
                continue
            items = [base] if query.kind == "base" else base["documents"]
            for item in items:
                review = review_state(item)
                # 旧记录没有真实申请，不伪造审核人或提交时间进入审核队列。
                if review.get("legacy"):
                    continue
                rows.append(
                    {
                        **review,
                        "id": item["id"],
                        "baseId": base["id"],
                        "baseName": base["name"],
                        "name": item["name"],
                        "kind": query.kind,
                        "owner": base["owner"],
                        "revision": base["revision"],
                    }
                )
        rows = [
            row
            for row in rows
            if row["kind"] == query.kind
            and query.q.casefold()
            in (row["name"] + " " + row["baseName"] + " " + row.get("owner", "")).casefold()
        ]
        counts = {
            status: sum(row["status"] == status for row in rows)
            for status in ("pending", "approved", "rejected", "needs_changes")
        }
        rows = [row for row in rows if not query.status or row["status"] == query.status]
        rows.sort(
            key=lambda row: row.get("reviewedAt") or row.get("submittedAt") or "", reverse=True
        )
        return {**paginate(rows, query.page, query.page_size), "counts": counts}

    def document(self, base: dict, document_id: str) -> dict:
        """只在已授权知识库中定位文档，不能将存储编号当作全局下载凭据。"""
        item = next((doc for doc in base["documents"] if doc["id"] == document_id), None)
        if item is None:
            raise HTTPException(404, "知识文档不存在")
        return item

    def preview(self, base_id: str, document_id: str, actor: Actor) -> dict:
        """管理员检查待审原文；截取预览并说明截断，完整资料可通过受控下载检查。"""
        base = self.service.record(base_id, actor, admin=True)
        doc = self.document(base, document_id)
        content = parse_document(doc["name"], self.service.files.read(doc["id"]))
        return {"name": doc["name"], "text": content[:20000], "truncated": len(content) > 20000}

    def decide(
        self,
        base_id: str,
        document_id: str | None,
        payload: ReviewInput,
        actor: Actor,
        reports: list[dict] | None = None,
    ) -> dict:
        """仅处理待审核项；文档通过前必须先批准所属知识库，批准后异步增量索引。"""
        base = self.service.record(base_id, actor, admin=True, edit=True)
        self.service._check_revision(base, payload.revision)
        item = self.document(base, document_id) if document_id else base
        if review_state(item)["status"] != "pending":
            raise HTTPException(409, "申请已处理或尚未提交，请刷新后查看")
        if document_id and payload.decision == "approved" and not is_approved(base):
            raise HTTPException(409, "请先通过知识库创建审批，再批准文档")
        if not document_id and payload.decision == "approved" and payload.scores is None:
            raise HTTPException(422, "通过前请完成七项质量评分")
        item["review"] = {
            **review_state(item),
            "status": payload.decision,
            "reason": payload.reason,
            "reviewer": actor.name,
            "reviewedAt": datetime.now(UTC).isoformat(),
        }
        base.setdefault("reviewHistory", []).append(
            {
                **item["review"],
                "id": item["id"],
                "name": item["name"],
                "kind": "document" if document_id else "base",
                "owner": base["owner"],
                "reviewerId": actor.id,
                "revision": payload.revision + 1,
                "scores": payload.scores.model_dump(by_alias=True) if payload.scores else None,
                "reportFiles": reports or [],
            }
        )
        action = ("文档审核：" if document_id else "创建审核：") + payload.decision
        if document_id and payload.decision == "approved":
            item["status"] = "pending"
            return self.service._start_job(base, payload.revision, actor, action, pending_only=True)
        return self.service._save(base, payload.revision, actor, action)

    def resubmit(
        self, base_id: str, payload: ResubmitInput, actor: Actor, *, admin: bool = False
    ) -> dict:
        """创建者修改被退回的资料后再次提交；保留历次审核意见供后台追溯。"""
        base = self.service.record(base_id, actor, admin=admin, edit=True)
        self.service._check_revision(base, payload.revision)
        if review_state(base)["status"] not in ("rejected", "needs_changes"):
            raise HTTPException(409, "仅被退回的创建申请可以修改后重新提交")
        self.service._set_config(base, payload.index_config)
        base.update(payload.model_dump(by_alias=True, exclude={"revision", "index_config"}))
        base["review"] = pending_review(actor)
        return self.service._save(base, payload.revision, actor, "修改资料并重新提交创建审批")
