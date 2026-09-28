"""知识库创建审批复用公共质量报告传输协议。"""

from fastapi import HTTPException, Request

from app.modules.common.identity import Actor, can_manage
from app.modules.common.memory_reports import report_router
from app.modules.knowledge.service import knowledge_service


def record(request: Request, identifier: str, actor: Actor) -> dict:
    """按当前创建申请编号定位知识库，并再次校验管理员管理范围。"""
    service = knowledge_service(request)
    for base in service.repository.list():
        if base.get("publicationId", base["id"]) == identifier and can_manage(actor, base):
            return {
                **base,
                "id": identifier,
                "revision": base.get("reviewRevision", base["revision"]),
                "status": base["review"]["status"],
                "reviews": [
                    {"reportFiles": item.get("reportFiles", [])}
                    for item in base.get("reviewHistory", [])
                    if item.get("kind") == "base"
                ],
            }
    raise HTTPException(404, "知识库创建申请不存在或无权查看")


router = report_router("knowledge", record)
