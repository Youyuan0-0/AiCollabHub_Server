"""可选质量评测报告的暂存、审核关联与受控读取。"""

from __future__ import annotations

import logging
from hashlib import sha256
from io import BytesIO
from pathlib import PurePath
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException

from app.modules.common.review_report_rules import (
    MAX_REPORT_FILES,
    check_pending,
    checked_publication,
    draft_reports,
    public_report,
    readable_report,
    validate_reports,
)

__all__ = [
    "MAX_REPORT_FILES",
    "public_report",
    "checked_publication",
    "check_pending",
    "draft_reports",
    "validate_reports",
    "readable_report",
    "save_report",
    "remove_report",
]
from app.modules.common.document_content import DOCUMENT_LIMIT, inspect_document
from app.modules.common.identity import Actor
from app.storage.client import MinioStorage

if TYPE_CHECKING:
    from app.modules.applications.application_service import ApplicationService

logger = logging.getLogger(__name__)


def save_report(
    service: ApplicationService,
    storage: MinioStorage,
    actor: Actor,
    publication_id: str,
    revision: int,
    name: str,
    content: bytes,
) -> dict:
    """先上传不可变文件，再复核申请状态并登记索引；不长时间占用数据库连接。"""
    with service.repository.transaction(is_readonly=True) as session:
        record = checked_publication(session, publication_id, actor)
        check_pending(record, revision)
        if len(draft_reports(session, record, actor)) >= MAX_REPORT_FILES:
            raise HTTPException(422, "每次审核最多上传 3 个报告附件")
    if PurePath(name).suffix.lower() not in (".pdf", ".docx", ".txt", ".md"):
        raise HTTPException(422, "报告支持 PDF、DOCX、TXT、MD 文档")
    if len(content) > min(DOCUMENT_LIMIT, storage.max_upload_bytes):
        raise HTTPException(413, "报告文档超过上传限制，单个文件最大 20 MiB")
    _, content_type, preview = inspect_document(name, content)
    if preview:
        preview = preview.replace(
            "<title>教程阅读</title>".encode(), "<title>质量评测报告</title>".encode()
        )
    if preview and len(preview) > storage.max_upload_bytes:
        raise HTTPException(413, "报告预览超过文件服务限制，请改用 PDF")
    from app.modules.applications.application_service import timestamp

    identifier = str(uuid4())
    key = f"application-review-reports/{identifier}/original"
    preview_key = f"application-review-reports/{identifier}/preview" if preview else key
    file = {
        "id": identifier,
        "name": name,
        "size": len(content),
        "contentType": content_type,
        "publicationId": publication_id,
        "publicationRevision": revision,
        "applicationId": record.get("applicationId")
        or record.get("resourceId")
        or record.get("datasetId")
        or record.get("toolId"),
        "reviewId": None,
        "createdBy": actor.id,
        "createdAt": timestamp(),
        "bucket": storage.bucket,
        "objectKey": key,
        "previewKey": preview_key,
        "previewContentType": "text/html" if preview else content_type,
        "previewSize": len(preview) if preview else None,
        "previewHash": sha256(preview).hexdigest() if preview else None,
        "contentHash": sha256(content).hexdigest(),
    }
    written = []
    is_registering = False
    try:
        storage.put_object(key, BytesIO(content), length=len(content), content_type=content_type)
        written.append(key)
        if preview:
            storage.put_object(
                preview_key, BytesIO(preview), length=len(preview), content_type="text/html"
            )
            written.append(preview_key)
        with service.repository.transaction() as session:
            latest = checked_publication(session, publication_id, actor)
            check_pending(latest, revision)
            if len(draft_reports(session, latest, actor)) >= MAX_REPORT_FILES:
                raise HTTPException(422, "每次审核最多上传 3 个报告附件")
            # 提交异常可能表示结果未知；此时保留对象，避免删掉已成功登记的文件。
            is_registering = True
            session.put_entry("review_files", identifier, file)
    except Exception:
        if not is_registering:
            for object_key in written:
                try:
                    storage.remove_object(object_key)
                except Exception:
                    logger.warning("Unregistered review report cleanup failed")
        raise
    return public_report(file)


def remove_report(
    service: ApplicationService,
    storage: MinioStorage,
    publication_id: str,
    identifier: str,
    actor: Actor,
) -> None:
    """移除本人暂存报告；审核已提交的报告保留，不能删除或覆盖。"""
    with service.repository.transaction() as session:
        checked_publication(session, publication_id, actor)
        file = session.entry("review_files", identifier)
        if not file or file["publicationId"] != publication_id or file["createdBy"] != actor.id:
            raise HTTPException(404, "报告不存在或无权移除")
        if file["reviewId"] is not None:
            raise HTTPException(409, "报告已随审核提交，不能移除")
        if file["bucket"] != storage.bucket:
            raise HTTPException(503, "报告存储配置已变化，请联系管理员")
        session.remove_review_file(identifier)
    if hasattr(service.repository, "foundation"):
        # 新库由共用文件清理任务核对所有引用后回收，不能在此破坏历史预览。
        return
    # 先解除索引再回收对象，数据库回滚时不会出现索引指向已删除文件。
    for key in {file["objectKey"], file["previewKey"]}:
        try:
            storage.remove_object(key)
        except Exception:
            logger.warning("Detached review report object requires cleanup: %s", identifier)
