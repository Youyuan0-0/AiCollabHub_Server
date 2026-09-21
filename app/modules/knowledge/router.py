"""知识库用户与管理路由；复用平台身份、CSRF、目录目标及分页协议。"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Request, UploadFile
from fastapi.responses import Response

from app.modules.common.identity import current_actor, require_admin
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.schemas import PageQuery
from app.modules.common.visibility import validate_visibility_targets, visibility_directory
from app.modules.knowledge.reviews import KnowledgeReviews
from app.modules.knowledge.schemas import (
    AdminInput,
    AdminUpdate,
    ChunkQuery,
    CreateInput,
    IndexInput,
    RebuildInput,
    ResubmitInput,
    RetrievalInput,
    ReviewInput,
    ReviewQuery,
    SettingsInput,
    VisibilityInput,
)
from app.modules.knowledge.service import knowledge_service


def build_router(admin: bool) -> APIRouter:
    """两端共用文档与检索流程，但身份入口、字段投影及管理权限始终由服务端固定。"""
    result = APIRouter(
        prefix=("/admin" if admin else "") + "/knowledge-bases",
        tags=["knowledge"],
    )

    @result.get("", response_model=None)
    def bases(request: Request, query: Annotated[PageQuery, Query()]) -> list[dict] | dict:
        """用户端保留列表协议；管理端按权限过滤后返回分页目录。"""
        return knowledge_service(request).catalog(
            current_actor(request, admin), query if admin else None, admin=admin
        )

    if admin:

        @result.post("/{base_id}/resubmit", response_model=None)
        def resubmit_admin(base_id: str, payload: ResubmitInput, request: Request) -> dict:
            """管理员也必须重新送审被退回的创建申请，不能通过保存资料绕过结论。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            return service.present(
                KnowledgeReviews(service).resubmit(base_id, payload, actor, admin=True),
                actor,
                admin=True,
            )

        @result.get("/reviews", response_model=None)
        def reviews(request: Request, query: Annotated[ReviewQuery, Query()]) -> dict:
            """创建与文档审核队列，共用场景应用的分页和状态约定。"""
            return KnowledgeReviews(knowledge_service(request)).queue(
                current_actor(request, True), query
            )

        @result.get("/audit", response_model=None)
        def audit(request: Request, query: Annotated[ReviewQuery, Query()]) -> dict:
            """查询已发生的审批结论，文档删除或重新提交不会抹除记录。"""
            return KnowledgeReviews(knowledge_service(request)).queue(
                current_actor(request, True), query, audit=True
            )

        @result.post("/{base_id}/review", response_model=None)
        def review_base(base_id: str, payload: ReviewInput, request: Request) -> dict:
            """审核创建申请，审批通过不自动扩大开放范围。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            return service.present(
                KnowledgeReviews(service).decide(base_id, None, payload, actor), actor, admin=True
            )

        @result.post("/{base_id}/documents/{document_id}/review", response_model=None)
        def review_document(
            base_id: str,
            document_id: str,
            payload: ReviewInput,
            request: Request,
            background: BackgroundTasks,
        ) -> dict:
            """文档批准后才启动索引，返回待处理状态供两端轮询。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            record = KnowledgeReviews(service).decide(base_id, document_id, payload, actor)
            if record.get("jobId"):
                background.add_task(service.process, base_id, record["jobId"])
            return service.present(record, actor, admin=True)

        @result.get("/{base_id}/documents/{document_id}/preview", response_model=None)
        def preview(base_id: str, document_id: str, request: Request) -> dict:
            """待审正文只向有权管理所属知识库的管理员提供。"""
            return KnowledgeReviews(knowledge_service(request)).preview(
                base_id, document_id, current_actor(request, True)
            )

        @result.get("/{base_id}/documents/{document_id}/original")
        def original(base_id: str, document_id: str, request: Request) -> Response:
            """原文作为附件下载，禁止浏览器以内联 HTML 执行上传内容。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            base = service.record(base_id, actor, admin=True)
            doc = KnowledgeReviews(service).document(base, document_id)
            return Response(
                service.files.read(doc["id"]),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(doc["name"], safe=""),
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

        @result.post("", status_code=201, response_model=None)
        def create_admin(payload: AdminInput, request: Request) -> dict:
            """后台新建含数据分级的完整资料，系统归属仍取管理员身份。"""
            actor = current_actor(request, True)
            require_admin(actor)
            service = knowledge_service(request)
            return service.present(
                service.create(payload, actor, publication_defaults(request, actor)),
                actor,
                admin=True,
            )

        @result.put("/{base_id}", response_model=None)
        def update(base_id: str, payload: AdminUpdate, request: Request) -> dict:
            """按修订保存管理字段，更新失败时不会覆盖已有记录。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            return service.present(service.update(base_id, payload, actor), actor, admin=True)

        @result.put("/{base_id}/visibility", response_model=None)
        def visibility(base_id: str, payload: VisibilityInput, request: Request) -> dict:
            """只有管理端能保存开放范围，沿用已有目录的目标存在性校验。"""
            actor, service = current_actor(request, True), knowledge_service(request)
            service.record(base_id, actor, admin=True, edit=True)
            validate_visibility_targets(request, actor, payload)
            return service.present(service.visibility(base_id, payload, actor), actor, admin=True)
    else:

        @result.post("/{base_id}/resubmit", response_model=None)
        def resubmit(base_id: str, payload: ResubmitInput, request: Request) -> dict:
            """申请人修改被退回的创建资料并重新送审。"""
            actor, service = current_actor(request), knowledge_service(request)
            return service.present(
                KnowledgeReviews(service).resubmit(base_id, payload, actor), actor
            )

        @result.post("", status_code=201, response_model=None)
        def create(payload: CreateInput, request: Request) -> dict:
            """创建初始私有知识库，浏览器不能设置开放范围或系统统计。"""
            actor, service = current_actor(request), knowledge_service(request)
            return service.present(
                service.create(payload, actor, publication_defaults(request, actor)), actor
            )

    @result.get("/{base_id}", response_model=None)
    def detail(base_id: str, request: Request) -> dict:
        """读取最新详情及处理状态；管理员额外获得开放目标目录。"""
        actor, service = current_actor(request, admin), knowledge_service(request)
        value = service.present(service.record(base_id, actor, admin=admin), actor, admin=admin)
        if admin:
            value["visibilityDirectory"] = visibility_directory(request, actor)
        return value

    @result.get("/{base_id}/chunks", response_model=None)
    def chunks(base_id: str, request: Request, query: Annotated[ChunkQuery, Query()]) -> dict:
        """按文档名和关键词查询当前库分段，先检查可见范围。"""
        return knowledge_service(request).chunks(
            base_id, query, current_actor(request, admin), admin=admin
        )

    @result.post("/{base_id}/documents", status_code=202, response_model=None)
    def upload(
        base_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
        revision: Annotated[int, Form(ge=1)],
    ) -> dict:
        """接收真实 multipart 文件并提交审批；审批通过后才开始索引。"""
        actor, service = current_actor(request, admin), knowledge_service(request)
        record = service.upload(base_id, files, revision, actor, admin=admin)
        return service.present(record, actor, admin=admin)

    @result.delete("/{base_id}/documents", response_model=None)
    def delete_document(
        base_id: str,
        request: Request,
        name: str,
        revision: Annotated[int, Query(ge=1)],
    ) -> dict:
        """删除文档时同步删除可检索片段，旧修订不得覆盖新上传。"""
        actor, service = current_actor(request, admin), knowledge_service(request)
        return service.present(
            service.delete_document(base_id, name, revision, actor, admin=admin), actor, admin=admin
        )

    @result.put("/{base_id}/settings", response_model=None)
    def settings(base_id: str, payload: SettingsInput, request: Request) -> dict:
        """保存索引配置并返回是否需要重新构建，禁止继续召回失效索引。"""
        actor, service = current_actor(request, admin), knowledge_service(request)
        config = IndexInput(**payload.model_dump(exclude={"revision"}))
        return service.present(
            service.settings(base_id, config, payload.revision, actor, admin=admin),
            actor,
            admin=admin,
        )

    @result.post("/{base_id}/rebuild", status_code=202, response_model=None)
    def rebuild(
        base_id: str, payload: RebuildInput, request: Request, background: BackgroundTasks
    ) -> dict:
        """登记新的重建任务；失败任务及过期任务均可重试。"""
        actor, service = current_actor(request, admin), knowledge_service(request)
        record = service.rebuild(
            base_id, payload.revision, actor, admin=admin, failed_only=payload.failed_only
        )
        background.add_task(service.process, base_id, record["jobId"])
        return service.present(record, actor, admin=admin)

    @result.post("/{base_id}/retrieval", response_model=None)
    def retrieve(base_id: str, payload: RetrievalInput, request: Request) -> list[dict]:
        """执行真实召回，仅使用当前已经构建的索引。"""
        return knowledge_service(request).retrieve(
            base_id, payload, current_actor(request, admin), admin=admin
        )

    return result


router = build_router(False)
admin_router = build_router(True)
