"""知识库模拟目录与维护接口，权限和业务状态保存在同一服务实例。"""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import ValidationError

from app.modules.common.fixtures import paginate
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.service import timestamp
from app.modules.knowledge.engine_workspace import EngineKnowledgeWorkspace
from app.modules.knowledge.schemas import (
    ChunkQuery,
    CreateInput,
    IndexInput,
    RetrievalInput,
    UploadInput,
)
from app.modules.knowledge.workspace import knowledge_workspace, public_knowledge

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge (simulation)"])


@router.get("/options")
def options(request: Request) -> dict:
    """返回创建知识库可选的真实模型与分段配置，避免前端固定演示模型。"""
    current_actor(request)
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        return workspace.engine.options()
    return {"mode": "disabled", "indexProfiles": []}


@router.get("")
def bases(request: Request) -> list[dict]:
    """查询可见知识库与本人草稿，公开卡片不含后台分级和范围。"""
    actor = current_actor(request)
    return [public_knowledge(item, actor) for item in knowledge_workspace(request).catalog(actor)]


@router.post("", status_code=201)
def create(payload: CreateInput, request: Request) -> dict:
    """创建保存在模拟仓储中的本人知识库。"""
    actor = current_actor(request)
    return public_knowledge(
        knowledge_workspace(request).create(payload, actor, publication_defaults(request, actor)),
        actor,
    )


@router.get("/{base_id}/chunks")
def chunks(base_id: str, request: Request, query: Annotated[ChunkQuery, Query()]) -> dict:
    """分页展示供应方模拟片段，不在平台实现文本切分。"""
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        return workspace.read_chunks(base_id, current_actor(request), query)
    item = workspace.resource(base_id, current_actor(request))
    values = [
        {
            "id": doc["name"],
            "documentName": doc["name"],
            "index": index + 1,
            "text": "模拟片段：" + doc["name"],
            "characterCount": len(doc["name"]),
        }
        for index, doc in enumerate(item["documents"])
        if (not query.document_name or query.document_name == doc["name"])
        and query.q.casefold() in doc["name"].casefold()
    ]
    return paginate(values, query.page, query.page_size)


@router.post("/{base_id}/documents")
def upload(base_id: str, payload: UploadInput, request: Request) -> dict:
    """模拟登记文档并更新真实元信息汇总，限制在契约层独立校验。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        raise HTTPException(422, "知识引擎需要文件正文，请使用文件上传接口")
    with workspace.lock:
        item = workspace.resource(base_id, actor)
        added = workspace.provider.documents(payload)
        names = {doc["name"] for doc in added}
        documents = added + [doc for doc in item["documents"] if doc["name"] not in names]
        item = workspace.edit(
            base_id,
            actor,
            {
                "indexConfig": payload.config.model_dump(by_alias=True)
                if payload.config
                else item["indexConfig"],
                "documents": documents,
                "documentCount": len(documents),
                "chunkCount": sum(doc.get("chunkCount", 0) for doc in documents),
                "totalSize": sum(doc.get("sizeBytes", 0) for doc in documents),
                "uploadedAt": timestamp(),
                "status": "ready",
                "indexBuiltAt": timestamp(),
            },
        )
        return public_knowledge(item, actor)


@router.delete("/{base_id}/documents")
def delete_document(base_id: str, name: str, request: Request) -> dict:
    """移除文档并同步数量、容量和分段统计。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        return public_knowledge(workspace.delete_file(base_id, actor, name), actor)
    with workspace.lock:
        item = workspace.resource(base_id, actor)
        docs = [doc for doc in item["documents"] if doc["name"] != name]
        item = workspace.edit(
            base_id,
            actor,
            {
                "documents": docs,
                "documentCount": len(docs),
                "totalSize": sum(doc.get("sizeBytes", 0) for doc in docs),
                "chunkCount": sum(doc.get("chunkCount", 0) for doc in docs),
            },
        )
        return public_knowledge(item, actor)


@router.put("/{base_id}/settings")
def settings(base_id: str, payload: IndexInput, request: Request) -> dict:
    """保存按字符校验的索引参数并记录版本。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        with workspace.operation(base_id, actor):
            raise HTTPException(422, "当前指南未提供现有知识库索引参数的修改契约，请联系供应方确认")
    return public_knowledge(
        knowledge_workspace(request).edit(
            base_id, actor, {"indexConfig": payload.model_dump(by_alias=True)}
        ),
        actor,
    )


@router.post("/{base_id}/rebuild")
def rebuild(base_id: str, request: Request) -> dict:
    """演练重建完成状态，真实索引构建由供应方适配器负责。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    if isinstance(workspace, EngineKnowledgeWorkspace):
        with workspace.operation(base_id, actor):
            raise HTTPException(422, "当前指南未提供独立重建接口，请联系供应方确认")
    return public_knowledge(
        knowledge_workspace(request).edit(
            base_id, actor, {"status": "ready", "indexBuiltAt": timestamp()}
        ),
        actor,
    )


@router.post("/{base_id}/retrieval")
def retrieve(base_id: str, payload: RetrievalInput, request: Request) -> list[dict]:
    """传递检索开关，真实和模拟调用复用权限、耗时及成功率统计。"""
    return knowledge_workspace(request).search(base_id, current_actor(request), payload)


@router.post("/{base_id}/files")
def upload_files(
    base_id: str,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    config: Annotated[str, Form()] = "{}",
) -> dict:
    """接收实际文件字节；开发兼容模式仍保留原元信息模拟契约。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    try:
        index = IndexInput.model_validate_json(config)
    except ValueError:
        raise HTTPException(422, "知识库索引配置格式不正确") from None
    try:
        if isinstance(workspace, EngineKnowledgeWorkspace):
            return public_knowledge(
                workspace.upload_files(
                    base_id,
                    actor,
                    files,
                    index,
                    request.app.state.knowledge_storage,
                ),
                actor,
            )
        from app.modules.knowledge.schemas import DocumentInput

        metadata = []
        for file in files:
            file.file.seek(0, 2)
            metadata.append(DocumentInput(name=file.filename or "", size=file.file.tell()))
        return upload(base_id, UploadInput(files=metadata, config=index), request)
    except ValidationError:
        raise HTTPException(422, "文件格式、大小或数量不符合上传要求") from None
    finally:
        for file in files:
            file.file.close()


@router.get("/{base_id}/status")
def processing_status(base_id: str, request: Request) -> dict:
    """刷新处理状态并返回完整可见快照，供前端按需轮询。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    item = (
        workspace.refresh(base_id, actor)
        if isinstance(workspace, EngineKnowledgeWorkspace)
        else workspace.resource(base_id, actor)
    )
    return public_knowledge(item, actor)


@router.post("/{base_id}/submit")
def submit(base_id: str, request: Request) -> dict:
    """提交知识库资料审核；数据分级由管理员补充，用户无权填入。"""
    actor = current_actor(request)
    workspace = knowledge_workspace(request)
    with workspace.lock:
        item = workspace.resource(base_id, actor)
        if item.get("createdBy") != actor.id:
            raise HTTPException(403, "只有发布者可以提交知识库审核")
        if isinstance(workspace, EngineKnowledgeWorkspace):
            workspace.ensure_ready_for_submission(base_id, actor)
        identifier = item.get("publicationId")
        old = workspace.records.get(identifier) if identifier else None
        if old and old["status"] == "pending":
            raise HTTPException(409, "已提交审核")
        data = {
            key: value
            for key, value in item.items()
            if key
            not in (
                "visibility",
                "createdBy",
                "organizationId",
                "versions",
                "listingStatus",
                "publicationId",
                "revision",
                "publicationStatus",
            )
        }
        if old and old["status"] == "approved":
            old = None
        record = workspace.save(
            data,
            actor,
            submit=True,
            identifier=old["id"] if old else None,
            revision=old["revision"] if old else None,
            resource_id=base_id,
        )
        workspace.resources[base_id]["publicationId"] = record["id"]
        workspace.resources[base_id]["publicationStatus"] = "pending"
        return record
