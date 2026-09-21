"""工具质量报告复用应用文件校验与审核归属规则；文件内容仅保存在内存。"""

from pathlib import PurePath
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.modules.common.document_content import DOCUMENT_LIMIT, inspect_document
from app.modules.common.file_transfer import limited_upload_form
from app.modules.common.identity import current_actor
from app.modules.common.review_report_rules import (
    MAX_REPORT_FILES,
    check_pending,
    checked_publication,
    draft_reports,
    public_report,
    readable_report,
)
from app.modules.common.service import timestamp
from app.modules.tools.workspace import tool_workspace

router = APIRouter(prefix="/admin/tool-publications", tags=["tool review reports"])


@router.get("/{publication_id}/report-files")
def reports(publication_id: str, request: Request) -> list[dict]:
    """读取当前审核员在本次申请修订内暂存的附件。"""
    actor = current_actor(request, True)
    with tool_workspace(request).repository.transaction(is_readonly=True) as session:
        record = checked_publication(session, publication_id, actor)
        return [public_report(file) for file in draft_reports(session, record, actor)]


@router.post("/{publication_id}/report-files", status_code=201)
async def upload(publication_id: str, request: Request, revision: int = Query(ge=1)) -> dict:
    """有界接收文档并验证真实内容，写入前再次检查申请状态与附件数量。"""
    actor = current_actor(request, True)
    workspace = tool_workspace(request)
    with workspace.repository.transaction(is_readonly=True) as session:
        check_pending(checked_publication(session, publication_id, actor), revision)
    async with limited_upload_form(
        request,
        request.app.state.tutorial_upload_limiter,
        max_bytes=DOCUMENT_LIMIT + 64 * 1024,
        max_fields=0,
        too_large_message="报告文档不能超过 20 MiB",
        invalid_form_message="每次请选择一个报告文件",
    ) as form:
        file = form.get("file")
        if len(form.multi_items()) != 1 or not isinstance(file, UploadFile) or not file.filename:
            raise HTTPException(422, "请选择一个报告文件")
        if PurePath(file.filename).suffix.lower() not in (".pdf", ".docx", ".txt", ".md"):
            raise HTTPException(422, "报告支持 PDF、DOCX、TXT、MD 文档")
        content = await file.read(DOCUMENT_LIMIT + 1)
        if len(content) > DOCUMENT_LIMIT:
            raise HTTPException(413, "报告文档不能超过 20 MiB")
        _, content_type, preview = await run_in_threadpool(inspect_document, file.filename, content)
        if preview:
            preview = preview.replace("教程阅读".encode(), "质量评测报告".encode())
        record = {
            "id": str(uuid4()),
            "name": PurePath(file.filename).name,
            "size": len(content),
            "contentType": content_type,
            "createdAt": timestamp(),
            "createdBy": actor.id,
            "publicationId": publication_id,
            "publicationRevision": revision,
            "reviewId": None,
            "content": content,
            "preview": preview,
        }
        with workspace.repository.transaction() as session:
            publication = checked_publication(session, publication_id, actor)
            check_pending(publication, revision)
            if len(draft_reports(session, publication, actor)) >= MAX_REPORT_FILES:
                raise HTTPException(422, "每次审核最多上传 3 个报告附件")
            session.put_entry("review_files", record["id"], record)
        return public_report(record)


@router.delete("/{publication_id}/report-files/{identifier}", status_code=204)
def remove(publication_id: str, identifier: str, request: Request) -> Response:
    """只能移除本人未提交报告；历史审核报告不可删除。"""
    actor = current_actor(request, True)
    with tool_workspace(request).repository.transaction() as session:
        checked_publication(session, publication_id, actor)
        file = session.entry("review_files", identifier)
        if not file or file["publicationId"] != publication_id or file["createdBy"] != actor.id:
            raise HTTPException(404, "报告不存在或无权移除")
        if file["reviewId"] is not None:
            raise HTTPException(409, "报告已随审核提交，不能移除")
        session.remove_review_file(identifier)
    return Response(status_code=204)


@router.api_route("/{publication_id}/report-files/{identifier}/content", methods=["GET", "HEAD"])
def content(
    publication_id: str, identifier: str, request: Request, preview: bool = False
) -> Response:
    """受控读取内存附件；原件下载、预览均验证管理权限。"""
    file = readable_report(
        tool_workspace(request), publication_id, identifier, current_actor(request, True)
    )
    data = file["preview"] if preview and file["preview"] else file["content"]
    disposition = "inline" if preview else "attachment"
    filename = quote(file["name"], safe="")
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
        "Content-Security-Policy": (
            "sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; frame-ancestors 'self'"
        ),
        "Content-Length": str(len(data)),
    }
    return Response(
        b"" if request.method == "HEAD" else data,
        headers=headers,
        media_type="text/html" if preview and file["preview"] else file["contentType"],
    )
