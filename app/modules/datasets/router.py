from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from app.modules.applications.identity import current_actor
from app.modules.applications.identity_directory import publication_defaults
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.schemas import (
    DatasetRankingMetric,
    FavoriteDataset,
    RateDataset,
    SaveDataset,
)
from app.modules.datasets.workspace import dataset_workspace

router = APIRouter(tags=["datasets (development)"])

# 公开接口白名单与管理端完整资源分离，后续新增管理字段不会自动向普通用户开放。
PUBLIC_DATASET_FIELDS = frozenset(
    {
        "id",
        "kind",
        "name",
        "englishName",
        "description",
        "descriptionMarkdown",
        "owner",
        "category",
        "tags",
        "usage",
        "rating",
        "ratingCount",
        "updatedAt",
        "status",
        "badge",
        "tone",
        "version",
        "facets",
        "downloadCount",
        "favoriteCount",
        "lastDownloadedAt",
        "recordCount",
        "dataSize",
        "qualityScore",
        "updateFrequency",
        "preview",
        "schema",
        "fieldSchema",
        "language",
        "dataFormat",
        "timeCoverage",
        "spatialCoverage",
        "publicationSource",
        "publishedAt",
        "versions",
    }
)


def public_dataset(resource: dict) -> dict:
    """投影普通用户可查看的资料；范围、密级、联系人和内部服务地址仅留在后台。"""
    return {key: value for key, value in resource.items() if key in PUBLIC_DATASET_FIELDS}


@router.get("/datasets", response_model=None)
def resources(request: Request, query: Annotated[CatalogQuery, Query()]) -> dict:
    """校验用户会话后分页查询数据集及动态筛选选项。"""
    page = dataset_workspace(request).catalog(query, current_actor(request))
    return {**page, "items": [public_dataset(item) for item in page["items"]]}


@router.get("/datasets/rankings", response_model=None)
def rankings(
    request: Request,
    metric: DatasetRankingMetric = "downloads",
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> dict:
    """提供首页累计排行；校验可信身份，limit 为 1–100，指标为下载量或收藏量。"""
    return dataset_workspace(request).rankings(metric, limit, current_actor(request))


@router.get("/datasets/{dataset_id}", response_model=None)
def detail(dataset_id: str, request: Request) -> dict:
    """读取已上架数据集完整详情，不把申请草稿当作已发布资源。"""
    return public_dataset(dataset_workspace(request).resource(dataset_id, current_actor(request)))


@router.get("/datasets/{dataset_id}/rating", response_model=None)
def rating(dataset_id: str, request: Request) -> dict:
    """读取本人评分和汇总；身份、开放范围沿用数据集权限控制。"""
    return dataset_workspace(request).rating(dataset_id, current_actor(request))


@router.get("/datasets/{dataset_id}/ratings", response_model=None)
def ratings(dataset_id: str, request: Request) -> list[dict]:
    """读取有权访问的数据集评价列表，未评分用户不产生空评价。"""
    return dataset_workspace(request).ratings(dataset_id, current_actor(request))


@router.put("/datasets/{dataset_id}/rating", response_model=None)
def rate(dataset_id: str, payload: RateDataset, request: Request) -> dict:
    """幂等保存一份用户评分，更新评分不增加评价人数；复用登录及 CSRF 校验。"""
    actor = current_actor(request)
    workspace = dataset_workspace(request)
    workspace.interact(dataset_id, actor, rating=payload.rating, comment=payload.comment)
    return workspace.rating(dataset_id, actor)


@router.get("/dataset-activity", response_model=None)
def activity(request: Request) -> dict:
    """读取当前用户的收藏状态。"""
    return dataset_workspace(request).activity(current_actor(request))


@router.put("/datasets/{dataset_id}/favorite", response_model=None)
def favorite(dataset_id: str, payload: FavoriteDataset, request: Request) -> dict:
    """幂等收藏或取消收藏，并返回服务端最新统计。"""
    return dataset_workspace(request).interact(
        dataset_id, current_actor(request), favorite=payload.favorite
    )


@router.post("/datasets/{dataset_id}/sample-download")
def download_sample(dataset_id: str, request: Request) -> Response:
    """下载已公开的脱敏样例；不暴露存储地址、不冒充完整原始数据包。"""
    actor = current_actor(request)
    content = dataset_workspace(request).download_sample(dataset_id, actor)
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="dataset-sample.json"',
        },
    )


@router.get("/dataset-publication-defaults", response_model=None)
def defaults(request: Request) -> dict:
    """提供身份默认值与运行模式；工具链协议尚未接入，不提供虚假的导入成功。"""
    return {
        **publication_defaults(request, current_actor(request)),
        "storage": request.app.state.settings.datasets_storage,
        "toolchainAvailable": False,
    }


@router.get("/dataset-publications", response_model=None)
def publications(request: Request) -> list[dict]:
    """列出本人数据集发布记录供搜索、分页和继续编辑。"""
    return dataset_workspace(request).publications(current_actor(request))


@router.get("/dataset-publications/{publication_id}", response_model=None)
def publication(publication_id: str, request: Request) -> dict:
    """返回本人发布申请的完整表单与审核状态。"""
    return dataset_workspace(request).publication(publication_id, current_actor(request))


@router.post("/dataset-publications", status_code=201, response_model=None)
def create(payload: SaveDataset, request: Request) -> dict:
    """创建草稿或待审核申请，生成稳定编号以支持后续编辑。"""
    return dataset_workspace(request).save(payload, current_actor(request))


@router.put("/dataset-publications/{publication_id}", response_model=None)
def save(publication_id: str, payload: SaveDataset, request: Request) -> dict:
    """按修订号更新本人草稿；已提交的申请保持只读。"""
    return dataset_workspace(request).save(payload, current_actor(request), publication_id)
