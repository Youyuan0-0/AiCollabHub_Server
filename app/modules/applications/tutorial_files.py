"""教程附件的持久化、发布关联校验及按应用权限读取。"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException

from app.modules.applications.ports import ApplicationSession
from app.modules.applications.publication_schemas import TutorialFile
from app.modules.common.document_content import inspect_document
from app.modules.common.identity import Actor, can_edit, can_manage
from app.storage.client import MinioStorage

if TYPE_CHECKING:
    from app.modules.applications.application_service import ApplicationService

logger = logging.getLogger(__name__)


def public_file(record: dict) -> dict:
    """只返回展示所需的附件资料，不向浏览器暴露存储桶或内部键。"""
    return TutorialFile.model_validate(
        {key: record[key] for key in ("id", "name", "size", "contentType", "securityLevel")}
    ).model_dump(by_alias=True)


def save_file(
    service: ApplicationService,
    storage: MinioStorage,
    actor: Actor,
    name: str,
    content: bytes,
    level: str,
) -> dict:
    """先保存不可变原文件和预览，再在事务中登记元信息。"""
    from app.modules.applications.application_service import timestamp

    if actor.role not in ("publisher", "admin", "org_admin"):
        raise HTTPException(403, "当前账号没有上传教程的权限")
    if level not in ("public", "internal"):
        raise HTTPException(422, "请选择公开或内部资料，当前通道不接收涉密文件")
    kind, content_type, preview = inspect_document(name, content)
    if max(len(content), len(preview or b"")) > storage.max_upload_bytes:
        raise HTTPException(413, "文件超过当前文件服务的上传限制")
    identifier = str(uuid4())
    key = f"application-tutorials/{identifier}/original"
    preview_key = f"application-tutorials/{identifier}/preview" if preview else key
    # 存储成功而登记失败时清理本次生成的对象，不影响已有版本引用的文件。
    written = []
    is_registering = False
    try:
        storage.put_object(key, BytesIO(content), length=len(content), content_type=content_type)
        written.append(key)
        if preview is not None:
            storage.put_object(
                preview_key, BytesIO(preview), length=len(preview), content_type="text/html"
            )
            written.append(preview_key)
        record = {
            "id": identifier,
            "name": name,
            "size": len(content),
            "contentType": content_type,
            "kind": kind,
            "securityLevel": level,
            "createdBy": actor.id,
            "organizationId": actor.organization_id,
            "createdAt": timestamp(),
            "objectKey": key,
            "previewKey": preview_key,
            "previewContentType": "text/html" if preview else content_type,
        }
        is_registering = True
        with service.repository.transaction() as session:
            session.put_entry("tutorial_files", identifier, record)
    except Exception:
        # 提交结果不明时保留对象，避免数据库已提交而原文件被补偿删除。
        for object_key in [] if is_registering else written:
            try:
                storage.remove_object(object_key)
            except Exception:
                logger.warning("Unregistered tutorial object cleanup failed")
        raise
    return {**public_file(record), "kind": kind}


def validate_files(
    session: ApplicationSession, data: dict, actor: Actor, security_level: str
) -> None:
    """校验附件存在、归属、类型与密级，并用可信元信息替换客户端描述。"""
    for tutorial in data.get("tutorials", []):
        if tutorial.get("source") != "file":
            continue
        file = session.entry("tutorial_files", tutorial["attachment"]["id"])
        if file is None or not can_edit(actor, file):
            raise HTTPException(422, "教程文件不存在或当前账号无权使用，请重新上传")
        if file["kind"] != tutorial["kind"]:
            raise HTTPException(422, "教程类型与上传文件不一致，请重新选择")
        if security_level == "public" and file["securityLevel"] != "public":
            raise HTTPException(422, "公开应用不能关联内部教程文件，请调整密级或重新上传")
        tutorial["attachment"] = public_file(file)


def readable_file(
    service: ApplicationService,
    actor: Actor,
    identifier: str,
    publication_id: str | None,
    application_id: str | None,
    *,
    is_admin: bool = False,
) -> dict:
    """先验证草稿归属或已发布应用可见性，再返回确实被该资料引用的附件。"""
    if publication_id and application_id:
        raise HTTPException(422, "请指定一个教程所属资源")
    with service.repository.transaction(is_readonly=True) as session:
        file = session.entry("tutorial_files", identifier)
        if file is None:
            raise HTTPException(404, "教程文件不存在或无权查看")
        data = None
        if publication_id:
            record = session.get_publication(publication_id)
            if not record or not (
                can_manage(actor, record) if is_admin else can_edit(actor, record)
            ):
                raise HTTPException(404, "教程文件不存在或无权查看")
            data = record["data"]
        elif application_id:
            data = service.workflows.public_resource(session, application_id, actor)
        elif can_edit(actor, file):
            return file
        if data is None or not any(
            item.get("source") == "file" and item.get("attachment", {}).get("id") == identifier
            for item in data.get("tutorials", [])
        ):
            raise HTTPException(404, "教程文件不存在或无权查看")
        return file
