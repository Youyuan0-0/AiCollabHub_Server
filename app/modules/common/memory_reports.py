"""模拟审核附件公共层：按业务、申请、修订和审核员隔离，原件仅保存在内存。"""

import asyncio
from collections.abc import Callable
from copy import deepcopy
from threading import RLock
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.modules.common.document_content import DOCUMENT_LIMIT, inspect_document
from app.modules.common.file_transfer import limited_upload_form
from app.modules.common.identity import Actor, current_actor
from app.modules.common.review_report_rules import public_report
from app.modules.common.service import timestamp


class MemoryReports:
    """不依赖特定资源仓储的附件集合；真实文件服务在此替换。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.upload_limiter = asyncio.Semaphore(4)
        self.files: dict[str, dict] = {}

    def selected(self, kind: str, record: dict, actor: Actor, ids: list[str]) -> list[dict]:
        """提交前检查附件归属、修订和上传人，只返回公开元信息。"""
        with self.lock:
            result = []
            for identifier in ids:
                file = self.files.get(identifier)
                if not file or (
                    file["kind"],
                    file["publicationId"],
                    file["revision"],
                    file["actorId"],
                ) != (
                    kind,
                    record["id"],
                    record["revision"],
                    actor.id,
                ):
                    raise HTTPException(422, "附件不属于当前申请、修订或审核员")
                result.append(public_report(file))
            return result


def reports_store(request: Request) -> MemoryReports:
    """取得启动时创建的实例隔离附件集合。"""
    return request.app.state.resource_reports


def report_router(
    kind: str, get_record: Callable[[Request, str, Actor], dict], prefix: str = "/admin"
) -> APIRouter:
    """装配同一附件协议，业务传入自己的权限检查和申请读取函数。"""
    router = APIRouter(prefix=f"{prefix}/{kind}-publications", tags=[f"{kind} review reports"])

    @router.get("/{publication_id}/report-files")
    def files(publication_id: str, request: Request) -> list[dict]:
        """读取本审核员当前修订暂存文件，历史文件从审核快照读取。"""
        actor = current_actor(request, True)
        record = get_record(request, publication_id, actor)
        store = reports_store(request)
        if hasattr(store, "foundation"):
            return store.list(kind, publication_id, actor)
        with store.lock:
            return [
                public_report(file)
                for file in store.files.values()
                if (file["kind"], file["publicationId"], file["revision"], file["actorId"])
                == (kind, publication_id, record["revision"], actor.id)
            ]

    @router.post("/{publication_id}/report-files", status_code=201)
    async def upload(publication_id: str, request: Request, revision: int = Query(ge=1)) -> dict:
        """限制文件大小并检查内容，收到完整文件后再次确认申请状态。"""
        actor = current_actor(request, True)
        get_record(request, publication_id, actor)
        async with limited_upload_form(
            request,
            reports_store(request).upload_limiter,
            max_bytes=DOCUMENT_LIMIT + 65536,
            max_fields=0,
            too_large_message="报告不能超过 20 MiB",
            invalid_form_message="请选择一个报告文件",
        ) as form:
            file = form.get("file")
            if (
                not isinstance(file, UploadFile)
                or not file.filename
                or not file.filename.lower().endswith((".pdf", ".docx", ".txt", ".md"))
            ):
                raise HTTPException(422, "报告支持 PDF、DOCX、TXT、MD")
            name = file.filename
            content = await file.read(DOCUMENT_LIMIT + 1)
            if not content or len(content) > DOCUMENT_LIMIT:
                raise HTTPException(422, "请选择非空且不超过 20 MiB 的报告")
            _, content_type, preview = inspect_document(name, content)
        store = reports_store(request)
        if hasattr(store, "foundation"):
            return await run_in_threadpool(
                store.upload, kind, publication_id, revision, actor, name, content
            )
        with store.lock:
            record = get_record(request, publication_id, actor)
            if record["status"] != "pending" or record["revision"] != revision:
                raise HTTPException(409, "审核状态或修订已变化")
            current = files(publication_id, request)
            if len(current) >= 3:
                raise HTTPException(422, "每次审核最多 3 个报告")
            item = {
                "id": uuid4().hex,
                "name": name,
                "size": len(content),
                "contentType": content_type,
                "createdAt": timestamp(),
                "content": content,
                "preview": preview,
                "kind": kind,
                "publicationId": publication_id,
                "revision": revision,
                "actorId": actor.id,
            }
            store.files[item["id"]] = item
            return public_report(item)

    @router.get("/{publication_id}/report-files/{file_id}/content")
    def content(
        publication_id: str, file_id: str, request: Request, preview: bool = False
    ) -> Response:
        """校验管理权与当前暂存/历史关联，预览使用沙盒响应。"""
        actor = current_actor(request, True)
        record = get_record(request, publication_id, actor)
        store = reports_store(request)
        if hasattr(store, "foundation"):
            return store.content(kind, publication_id, file_id, actor, preview)
        with store.lock:
            item = deepcopy(store.files.get(file_id))
        historical = any(
            file_id == file["id"]
            for review in record.get("reviews", [])
            for file in review.get("reportFiles", [])
        )
        if (
            not item
            or item["kind"] != kind
            or item["publicationId"] != publication_id
            or not (
                historical
                or (item["revision"] == record["revision"] and item["actorId"] == actor.id)
            )
        ):
            raise HTTPException(404, "报告不存在或无权访问")
        body = item["preview"] if preview and item["preview"] else item["content"]
        disposition = "inline" if preview else "attachment"
        filename = quote(item["name"])
        return Response(
            body,
            media_type="text/html" if preview and item["preview"] else item["contentType"],
            headers={
                "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete("/{publication_id}/report-files/{file_id}", status_code=204)
    def remove(publication_id: str, file_id: str, request: Request) -> Response:
        """只能删除本人当前未提交的报告，历史报告不可移除。"""
        actor = current_actor(request, True)
        store = reports_store(request)
        if hasattr(store, "foundation"):
            store.remove(kind, publication_id, file_id, actor)
            return Response(status_code=204)
        with store.lock:
            record = get_record(request, publication_id, actor)
            if record["status"] != "pending":
                raise HTTPException(409, "历史报告不可删除")
            store.selected(kind, record, actor, [file_id])
            del store.files[file_id]
        return Response(status_code=204)

    return router
