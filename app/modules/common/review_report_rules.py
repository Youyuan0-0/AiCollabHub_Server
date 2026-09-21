"""事务型资源报告的权限与归属规则；存储方式由业务服务提供。"""

from typing import Protocol

from fastapi import HTTPException

from app.modules.common.identity import Actor, can_manage, require_admin
from app.modules.common.transaction_ports import ResourceRepository, ResourceSession

MAX_REPORT_FILES = 3


class ReportRepositoryOwner(Protocol):
    """报告操作只依赖公共仓储，不依赖具体资源服务。"""

    repository: ResourceRepository


def public_report(record: dict) -> dict:
    """返回报告展示信息，隐藏存储桶、对象键与上传人内部资料。"""
    return {key: record[key] for key in ("id", "name", "size", "contentType", "createdAt")}


def checked_publication(session: ResourceSession, publication_id: str, actor: Actor) -> dict:
    """审核报告只向有权管理所属申请的管理员开放。"""
    require_admin(actor)
    record = session.get_publication(publication_id)
    if not record or not can_manage(actor, record):
        raise HTTPException(404, "申请不存在或无权查看")
    return record


def check_pending(record: dict, revision: int) -> None:
    """阻止上传过程中申请已被审核或重新提交后误挂报告。"""
    if record["status"] != "pending" or record["revision"] != revision:
        raise HTTPException(409, "申请状态已变化，请刷新后重试")


def draft_reports(session: ResourceSession, record: dict, actor: Actor) -> list[dict]:
    """恢复当前审核员本轮已上传但尚未提交的报告。"""
    return [
        file
        for file in session.review_files(record["id"])
        if file["reviewId"] is None
        and file["publicationRevision"] == record["revision"]
        and file["createdBy"] == actor.id
    ]


def validate_reports(
    session: ResourceSession, record: dict, identifiers: list[str], actor: Actor
) -> list[dict]:
    """审核提交只允许关联本人在同一申请、同一修订轮次上传的报告。"""
    files = []
    for identifier in identifiers:
        file = session.entry("review_files", identifier)
        if not file or (
            file["createdBy"] != actor.id
            or file["publicationId"] != record["id"]
            or file["publicationRevision"] != record["revision"]
            or file["reviewId"] is not None
        ):
            raise HTTPException(422, "报告不存在、已被使用或不属于本次审核，请刷新后重新选择")
        files.append(file)
    return files


def readable_report(
    service: ReportRepositoryOwner, publication_id: str, identifier: str, actor: Actor
) -> dict:
    """历史报告按申请管理权限读取；暂存报告仅上传者本人可读取。"""
    with service.repository.transaction(is_readonly=True) as session:
        checked_publication(session, publication_id, actor)
        file = session.entry("review_files", identifier)
        if (
            not file
            or file["publicationId"] != publication_id
            or (file["reviewId"] is None and file["createdBy"] != actor.id)
        ):
            raise HTTPException(404, "报告不存在或无权查看")
        return file
