"""模型目录、专属发布和模型仓库成果导入接口。"""

import json
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from app.modules.common.catalog import query_resources
from app.modules.common.downloads import complete_download
from app.modules.common.identity import Actor, current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.schemas import CatalogQuery, Payload
from app.modules.common.toolchain.configuration import module_entry_url, module_settings
from app.modules.common.toolchain.contracts import authorize
from app.modules.models.schemas import SaveModel, validate_model
from app.modules.models.workspace import model_workspace, public_model

router = APIRouter(tags=["models (simulation)"])


class FavoriteInput(Payload):
    """模型收藏状态由服务端保存。"""

    favorite: bool


@router.get("/models")
def resources(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """搜索参数、子模型及单位，能力筛选与任务维度组合。"""
    items = model_workspace(request).catalog(current_actor(request))
    keyword = query.q.casefold()
    items = [
        item
        for item in items
        if keyword
        in " ".join(
            [
                item["name"],
                item.get("summary", ""),
                item.get("owner", ""),
                item.get("parameters", ""),
                item.get("taskType", ""),
                item.get("framework", ""),
                *item.get("tags", []),
                *item.get("submodels", []),
            ]
        ).casefold()
    ]
    for item in items:
        task_type = item.get("taskType", "")
        nlp_tasks = {"文本生成", "文本分类", "文本翻译"}
        speech_tasks = {"语音识别", "语音合成"}
        metadata = [
            {"label": label, "value": item.get(key) or "未提供"}
            for label, key in (
                ("模型架构", "architecture"),
                ("参数规模", "parameters"),
                ("上下文长度", "contextLength"),
                ("模型框架", "framework"),
                ("模型格式", "modelFormat"),
                ("权重精度", "precision"),
            )
        ]
        metrics = [
            {"label": label, "value": str(value)}
            for label, value in (
                ("质量评分", item.get("qualityScore")),
                ("基准测评", item.get("benchmarkScore")),
                ("领域测评", item.get("domainScore")),
                ("推理速度", item.get("inferenceSpeed")),
                ("显存要求", item.get("gpuMemory")),
            )
            if value not in (None, "")
        ]
        item.update(
            description=item.get("summary", ""),
            category=task_type,
            status="available",
            tone="blue",
            badge=item.get("modelStatus", ""),
            metadata=metadata,
            metrics=metrics,
            facets={
                "support": item.get("serviceCapabilities", []),
                "taskType": [task_type] if task_type else [],
                "modality": list(
                    set(item.get("inputModalities", []) + item.get("outputModalities", []))
                ),
                "nlpCapability": [task_type] if task_type in nlp_tasks else [],
                "speechCapability": [task_type] if task_type in speech_tasks else [],
            },
        )
    return query_resources(
        [public_model(item) for item in items], query.model_copy(update={"q": ""})
    )


@router.get("/models/{identifier}")
def detail(identifier: str, request: Request) -> dict:
    """返回授权范围内模型专属资料。"""
    return public_model(model_workspace(request).resource(identifier, current_actor(request)))


@router.put("/models/{identifier}/favorite")
def favorite(identifier: str, payload: FavoriteInput, request: Request) -> dict:
    """幂等更新模型收藏，不依赖浏览器本地状态。"""
    return public_model(
        model_workspace(request).interact(
            identifier, current_actor(request), favorite=payload.favorite
        )
    )


@router.post("/models/{identifier}/download")
def download(identifier: str, request: Request) -> dict:
    """模拟下载已发布配置，统计对应配置资料，不伪称下载权重。"""
    workspace, actor = model_workspace(request), current_actor(request)
    if hasattr(workspace, "repository"):
        request_id = str(uuid4())
        item = workspace.interact(identifier, actor, download=True, download_request_id=request_id)
        return JSONResponse(
            {"filename": "config.json", "content": json.loads(item.get("configJson") or "{}")},
            background=BackgroundTask(
                complete_download, workspace.repository, f"{actor.id}:{identifier}:{request_id}"
            ),
        )
    item = workspace.interact(identifier, actor, download=True)
    return {"filename": "config.json", "content": json.loads(item.get("configJson") or "{}")}


@router.get("/model-publication-defaults")
def defaults(request: Request) -> dict:
    """带出可信单位、联系人及模型工具链独立接入状态。"""
    actor = current_actor(request)
    settings = module_settings(request.app.state.settings, "models", "aliyun")
    return {
        **publication_defaults(request, actor),
        "toolchainMode": settings.toolchain_mode,
        "toolchainProvider": settings.toolchain_provider,
        "toolchainAvailable": settings.toolchain_mode != "disabled",
        "toolchainUrl": module_entry_url(request.app.state.settings, "models", "aliyun"),
    }


@router.get("/model-publications")
def publications(request: Request) -> list[dict]:
    """列出本人模型草稿和审核记录。"""
    return model_workspace(request).publications(current_actor(request))


@router.get("/model-publications/{identifier}")
def publication(identifier: str, request: Request) -> dict:
    """读取本人模型发布申请。"""
    return model_workspace(request).publication(identifier, current_actor(request))


def save_model(payload: SaveModel, request: Request, identifier: str | None = None) -> dict:
    """复用公共状态机，保留模型字段和必填校验。"""
    if payload.submit:
        validate_model(payload.data)
    actor = current_actor(request)
    data = {
        **payload.data.model_dump(by_alias=True),
        **publication_defaults(request, actor),
        "sourceType": "自研",
        "trainingDatasetIds": [],
        "securityLevel": "内部",
    }
    workspace = model_workspace(request)
    if identifier:
        old = workspace.publication(identifier, actor)
        for key in ("sourceType", "trainingDatasetIds", "securityLevel"):
            if key in old["data"]:
                data[key] = old["data"][key]
        if old["data"].get("source"):
            data["source"] = old["data"]["source"]
        resource = workspace.find_resource(old["resourceId"])
        if payload.submit and resource and data["version"] == resource.get("version"):
            raise HTTPException(422, "新版本号不能与当前已发布版本相同")
    return workspace.save(
        data, actor, submit=payload.submit, identifier=identifier, revision=payload.revision
    )


def model_toolchain_actor(request: Request) -> Actor:
    """按模型库独立连接配置检查发布身份及供应方账号授权。"""
    actor = current_actor(request)
    authorize(module_settings(request.app.state.settings, "models", "aliyun"), actor)
    return actor


@router.post("/model-publications", status_code=201)
def create(payload: SaveModel, request: Request) -> dict:
    """新建模型草稿或提交审核。"""
    return save_model(payload, request)


@router.put("/model-publications/{identifier}")
def update(identifier: str, payload: SaveModel, request: Request) -> dict:
    """按修订编辑退回或暂存的申请。"""
    return save_model(payload, request, identifier)


@router.get("/model-toolchain")
def toolchain(request: Request) -> dict:
    """读取已启用的模型成果；真实服务失败时不回退模拟目录。"""
    model_toolchain_actor(request)
    settings = module_settings(request.app.state.settings, "models", "aliyun")
    return {
        "mode": settings.toolchain_mode,
        "entryUrl": module_entry_url(request.app.state.settings, "models", "aliyun"),
        "items": request.app.state.model_provider.list_models(),
    }


@router.post("/model-toolchain/{identifier}/import")
def import_model(
    identifier: str,
    request: Request,
    version: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
) -> dict:
    """按供应方版本导入草稿；同来源复用申请，新版本沿用已发布资源。"""
    actor = model_toolchain_actor(request)
    workspace = model_workspace(request)
    source = request.app.state.model_provider.get_model(identifier, version)
    origin = source["source"]

    def same_origin(item: dict) -> bool:
        saved = item["data"].get("source") or {}
        return all(
            saved.get(key) == origin.get(key)
            for key in ("externalId", "environment", "region", "provider")
        )

    with workspace.lock:
        records = workspace.publications(actor)
        old = next(
            (
                item
                for item in records
                if same_origin(item) and item["data"]["source"].get("version") == origin["version"]
            ),
            None,
        )
        if old:
            return old
        previous = next(
            (item for item in records if same_origin(item) and item["status"] == "approved"),
            None,
        )
        if previous and any(
            item["resourceId"] == previous["resourceId"]
            and item["status"] in ("draft", "pending", "needs_changes")
            for item in records
        ):
            raise HTTPException(409, "此模型已有待处理的新版本，请先完成当前申请")
        data = {
            **source,
            **publication_defaults(request, actor),
            "sourceType": "第三方引入",
        }
        return workspace.save(data, actor, resource_id=previous["resourceId"] if previous else None)


@router.post("/model-publications/{identifier}/new-version", status_code=201)
def new_version(identifier: str, request: Request) -> dict:
    """从本人已通过版本建立独立草稿，保留资源编号与历史授权。"""
    actor = current_actor(request)
    workspace = model_workspace(request)
    with workspace.lock:
        old = workspace.publication(identifier, actor)
        if old["status"] != "approved":
            raise HTTPException(409, "只有已通过的模型可以创建新版本")
        pending = next(
            (
                item
                for item in workspace.publications(actor)
                if item["resourceId"] == old["resourceId"]
                and item["status"] in ("draft", "pending", "needs_changes")
            ),
            None,
        )
        if pending:
            return pending
        data = {
            **old["data"],
            "version": "",
            "source": {"kind": "manual", "basedOn": old["data"].get("source")},
        }
        return workspace.save(data, actor, resource_id=old["resourceId"])
