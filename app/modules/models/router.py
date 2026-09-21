"""模型目录、专属发布和工具链模拟接口。"""

import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.modules.common.catalog import query_resources
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.schemas import CatalogQuery, Payload
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
                *item.get("tags", []),
                *item.get("submodels", []),
            ]
        ).casefold()
    ]
    for item in items:
        item.update(
            description=item.get("summary", ""),
            category=item.get("taskType", ""),
            status="available",
            tone="blue",
            facets={
                "support": item.get("serviceCapabilities", []),
                "taskType": [item.get("taskType", "")],
                "modality": list(
                    set(item.get("inputModalities", []) + item.get("outputModalities", []))
                ),
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
    item = model_workspace(request).interact(identifier, current_actor(request), download=True)
    return {"filename": "config.json", "content": json.loads(item.get("configJson") or "{}")}


@router.get("/model-publication-defaults")
def defaults(request: Request) -> dict:
    """带出可信单位与联系人。"""
    return publication_defaults(request, current_actor(request))


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
        resource = workspace.resources.get(old["resourceId"])
        if payload.submit and resource and data["version"] == resource.get("version"):
            raise HTTPException(422, "新版本号不能与当前已发布版本相同")
    return workspace.save(
        data, actor, submit=payload.submit, identifier=identifier, revision=payload.revision
    )


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
    """返回模拟训推工具链入口和已完成模型。"""
    current_actor(request)
    return {
        "mode": "simulation",
        "entryUrl": "/models/toolchain?workspace=1",
        "items": request.app.state.model_provider.list_models(),
    }


@router.post("/model-toolchain/{identifier}/import")
def import_model(identifier: str, request: Request) -> dict:
    """拉取成果生成草稿，同账号与外部版本重复拉取幂等。"""
    actor = current_actor(request)
    workspace = model_workspace(request)
    source = next(
        (
            item
            for item in request.app.state.model_provider.list_models()
            if item["externalId"] == identifier
        ),
        None,
    )
    if source is None:
        raise HTTPException(404, "工具链模型不存在")
    with workspace.lock:
        old = next(
            (
                item
                for item in workspace.publications(actor)
                if item["data"].get("source")
                == {"externalId": identifier, "version": source["version"]}
            ),
            None,
        )
        if old:
            return old
        data = {
            **source,
            **publication_defaults(request, actor),
            "source": {"externalId": identifier, "version": source["version"]},
        }
        return workspace.save(data, actor)


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
        data = {**old["data"], "version": ""}
        return workspace.save(data, actor, resource_id=old["resourceId"])
