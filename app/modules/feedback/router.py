"""问题反馈的用户与后台接口，共用登录、数据库和 MinIO。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.db.session import SessionDependency
from app.modules.feedback import attachments, service
from app.modules.feedback.models import FeedbackRecord
from app.modules.feedback.schemas import (
    FEEDBACK_MODULES,
    FEEDBACK_TYPES,
    FILE_TYPES,
    MAX_ATTACHMENTS,
    MAX_FILE_BYTES,
    TEMPORARY_SECONDS,
    FeedbackInput,
    FeedbackQuery,
    FeedbackUpdate,
)
from app.storage.client import StorageDependency

router = APIRouter(tags=["feedback"])


@router.get("/feedback/config")
def configuration(request: Request) -> dict:
    """返回统一表单与上传限制，不暴露存储地址和账号。"""
    service.actor(request)
    return {
        "types": [{"value": key, "label": value} for key, value in FEEDBACK_TYPES.items()],
        "modules": [{"value": key, "label": value} for key, value in FEEDBACK_MODULES.items()],
        "maxAttachments": MAX_ATTACHMENTS,
        "maxFileBytes": MAX_FILE_BYTES,
        "extensions": list(FILE_TYPES),
        "temporaryRetentionSeconds": TEMPORARY_SECONDS,
        "securityLevels": [],
    }


class UploadLimitExceeded(MultiPartException):
    """使解析器在超出请求上限时关闭已创建的临时文件。"""


@router.post("/feedback/attachments", status_code=201)
async def upload(request: Request, db: SessionDependency, storage: StorageDependency) -> dict:
    """先鉴权、限制并发与接收字节，再解析单文件表单并流式保存。"""
    owner = service.actor(request)
    limiter = request.app.state.feedback_upload_limiter
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=0.1)
    except TimeoutError:
        raise HTTPException(429, "当前上传较多，请稍后重试") from None
    form = None
    parser = None
    count = 0

    async def limited_stream() -> AsyncIterator[bytes]:
        nonlocal count
        async for block in request.stream():
            count += len(block)
            if count > MAX_FILE_BYTES + 64 * 1024:
                raise UploadLimitExceeded("单个附件不能超过 1 GiB（1024 MiB）")
            yield block

    try:
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise HTTPException(415, "请使用文件上传表单")
        parser = MultiPartParser(
            request.headers, limited_stream(), max_files=1, max_fields=2, max_part_size=4096
        )
        try:
            form = await parser.parse()
        except UploadLimitExceeded as error:
            raise HTTPException(413, str(error)) from None
        except MultiPartException:
            raise HTTPException(422, "上传表单无效，每次请求只能上传一个文件") from None
        file = form.get("file")
        if not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "请选择一个附件")
        if file.size is not None and file.size > MAX_FILE_BYTES:
            raise HTTPException(413, "单个附件不能超过 1 GiB（1024 MiB）")
        try:
            request_id = str(UUID(str(form.get("clientRequestId", ""))))
        except ValueError:
            raise HTTPException(422, "上传请求编号无效") from None
        level = form.get("securityLevel")
        return await run_in_threadpool(
            attachments.save_upload,
            db,
            request.app.state.settings,
            storage,
            owner,
            request_id,
            file.filename,
            file.file,
            str(level) if level else None,
        )
    finally:
        if form is not None:
            await form.close()
        elif parser is not None:
            # 客户端中断等非 Multipart 异常也要关闭解析阶段创建的临时文件。
            for temporary in parser._files_to_close_on_error:
                temporary.close()
        limiter.release()


@router.get("/feedback/uploads/{request_id}")
def upload_status(request_id: UUID, request: Request, db: SessionDependency) -> dict:
    """核对当前账号的上传结果，超时重试继续使用原编号。"""
    item = attachments.find_upload(db, service.actor(request), str(request_id))
    if item is None:
        raise HTTPException(404, "尚未查到该上传记录")
    return service.attachment_value(item)


@router.delete("/feedback/attachments/{attachment_id}", status_code=204)
def remove_upload(
    attachment_id: UUID, request: Request, db: SessionDependency, storage: StorageDependency
) -> Response:
    """撤销自己的未提交附件；已绑定文件不允许删除。"""
    attachments.remove_temporary(
        db, request.app.state.settings, storage, service.actor(request), str(attachment_id)
    )
    return Response(status_code=204)


@router.get("/feedback/submissions/{request_id}")
def submission_status(request_id: UUID, request: Request, db: SessionDependency) -> dict:
    """查找本次提交是否已保存，避免断连后重复创建反馈。"""
    item = service.submission(db, service.actor(request), str(request_id))
    if item is None:
        raise HTTPException(404, "尚未查到该提交记录")
    return service.detail(db, item)


@router.get("/feedback")
def user_page(
    request: Request, db: SessionDependency, query: Annotated[FeedbackQuery, Query()]
) -> dict:
    """分页读取当前账号的反馈。"""
    return service.page(db, query, service.actor(request))


@router.post("/feedback")
def submit(
    request: Request, db: SessionDependency, payload: FeedbackInput, response: Response
) -> dict:
    """保存反馈及附件关联，幂等重放返回原记录。"""
    result, created = service.submit(
        db, request.app.state.settings, service.actor(request), payload
    )
    response.status_code = 201 if created else 200
    return result


@router.get("/feedback/{record_id}")
def user_detail(
    record_id: UUID,
    request: Request,
    db: SessionDependency,
    before_event_version: Annotated[int | None, Query(alias="beforeEventVersion", ge=1)] = None,
) -> dict:
    """读取自己的反馈和处理过程。"""
    item = service.owned_record(db, str(record_id), service.actor(request))
    return service.detail(db, item, before_event_version)


@router.get("/feedback/{record_id}/attachments/{attachment_id}/content", response_model=None)
def user_download(
    record_id: UUID,
    attachment_id: UUID,
    request: Request,
    db: SessionDependency,
    storage: StorageDependency,
) -> Response:
    """校验提交人后提供原文件下载。"""
    service.owned_record(db, str(record_id), service.actor(request))
    return attachments.download(db, storage, str(record_id), str(attachment_id))


@router.get("/admin/overview")
def overview(request: Request, db: SessionDependency) -> dict:
    """统计管理员授权范围内各状态反馈数量。"""
    service.actor(request, admin=True)
    counts = dict(
        db.execute(
            select(FeedbackRecord.status, func.count()).group_by(FeedbackRecord.status)
        ).all()
    )
    return {
        "total": sum(counts.values()),
        "open": counts.get("processing", 0),
        "resolved": counts.get("resolved", 0),
    }


@router.get("/admin/feedback")
def admin_page(
    request: Request, db: SessionDependency, query: Annotated[FeedbackQuery, Query()]
) -> dict:
    """分页筛选管理员可处理的反馈。"""
    return service.page(db, query, service.actor(request, admin=True), admin=True)


@router.get("/admin/feedback/{record_id}")
def admin_detail(
    record_id: UUID,
    request: Request,
    db: SessionDependency,
    before_event_version: Annotated[int | None, Query(alias="beforeEventVersion", ge=1)] = None,
) -> dict:
    """打开处理窗口时读取最新版本和历史。"""
    item = service.owned_record(db, str(record_id), service.actor(request, admin=True), admin=True)
    return service.detail(db, item, before_event_version)


@router.patch("/admin/feedback/{record_id}")
def process(
    record_id: UUID, request: Request, db: SessionDependency, payload: FeedbackUpdate
) -> dict:
    """追加处理说明，并以乐观锁更新反馈状态。"""
    return service.process(
        db, request.app.state.settings, service.actor(request, admin=True), str(record_id), payload
    )


@router.get("/admin/feedback/{record_id}/attachments/{attachment_id}/content", response_model=None)
def admin_download(
    record_id: UUID,
    attachment_id: UUID,
    request: Request,
    db: SessionDependency,
    storage: StorageDependency,
) -> Response:
    """后台重新鉴权后下载反馈附件。"""
    service.owned_record(db, str(record_id), service.actor(request, admin=True), admin=True)
    return attachments.download(db, storage, str(record_id), str(attachment_id))
