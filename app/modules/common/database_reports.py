"""五类资源共用的持久审核报告适配。"""

import asyncio
from collections.abc import Iterator
from contextlib import nullcontext
from urllib.parse import quote

from fastapi import HTTPException, Response
from fastapi.responses import StreamingResponse

from app.db.foundation import FoundationDatabase
from app.modules.applications.review_files import save_report
from app.modules.common.database_workspace import DatabaseResourceWorkspace
from app.modules.common.identity import Actor
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.review_report_rules import (
    checked_publication,
    draft_reports,
    public_report,
    readable_report,
    validate_reports,
)
from app.storage.client import MinioStorage


class DatabaseReports:
    """文件本体走 MinIO，当前轮次和不可变归档走资源关联表。"""

    def __init__(self, foundation: FoundationDatabase, storage: MinioStorage | None) -> None:
        self.foundation, self.storage = foundation, storage
        self.upload_limiter = asyncio.Semaphore(4)
        # 兼容路由的上下文接口；跨 worker 的审核和附件归档由数据库事务锁保护。
        self.lock = nullcontext()

    def _workspace(self, kind: str) -> DatabaseResourceWorkspace:
        return DatabaseResourceWorkspace(ResourceDatabase(self.foundation, kind))

    def selected(self, kind: str, record: dict, actor: Actor, ids: list[str]) -> list[dict]:
        """提交审核前校验附件归属，真正归档仍由审核事务完成。"""
        with self._workspace(kind).repository.transaction(is_readonly=True) as session:
            return [public_report(file) for file in validate_reports(session, record, ids, actor)]

    def list(self, kind: str, publication_id: str, actor: Actor) -> list[dict]:
        """只读取当前管理员在本轮申请中的暂存报告。"""
        with self._workspace(kind).repository.transaction(is_readonly=True) as session:
            record = checked_publication(session, publication_id, actor)
            return [public_report(file) for file in draft_reports(session, record, actor)]

    def upload(
        self, kind: str, publication_id: str, revision: int, actor: Actor, name: str, content: bytes
    ) -> dict:
        """复用有界内容校验及原文件、预览登记流程。"""
        if self.storage is None:
            raise HTTPException(503, "object_storage_not_configured")
        return save_report(
            self._workspace(kind), self.storage, actor, publication_id, revision, name, content
        )

    def remove(self, kind: str, publication_id: str, file_id: str, actor: Actor) -> None:
        """只撤销本轮暂存引用；对象清理使用文件生命周期。"""
        with self._workspace(kind).repository.transaction() as session:
            record = checked_publication(session, publication_id, actor)
            if record["status"] != "pending":
                raise HTTPException(409, "历史报告不可删除")
            validate_reports(session, record, [file_id], actor)
            session.remove_review_file(file_id)

    def content(
        self,
        kind: str,
        publication_id: str,
        file_id: str,
        actor: Actor,
        preview: bool,
        *,
        head: bool = False,
    ) -> Response:
        """检查申请权限后按块输出原件或沙盒预览。"""
        file = readable_report(self._workspace(kind), publication_id, file_id, actor)
        if self.storage is None or file["bucket"] != self.storage.bucket:
            raise HTTPException(503, "object_storage_not_configured")
        key = file["previewKey"] if preview else file["objectKey"]
        info = self.storage.stat_object(key)
        headers = {
            "Content-Length": str(info.size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Content-Disposition": ("inline" if preview else "attachment")
            + "; filename*=UTF-8''"
            + quote(file["name"], safe=""),
        }

        def blocks() -> Iterator[bytes]:
            with self.storage.open_object(key) as stream:
                yield from stream.stream(65536)

        media_type = file["previewContentType"] if preview else file["contentType"]
        return (
            Response(headers=headers, media_type=media_type)
            if head
            else StreamingResponse(blocks(), headers=headers, media_type=media_type)
        )
