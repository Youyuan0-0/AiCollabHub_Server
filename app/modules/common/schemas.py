from __future__ import annotations

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Payload(BaseModel):
    """平台请求模型基类，负责 Python 下划线字段与 camelCase JSON 的映射。"""

    # 把前端 JSON 的 descriptionMarkdown 等驼峰字段映射到 Python 的 description_markdown。
    # model_dump(by_alias=True) 再按驼峰名称导出，保持发送和返回时的字段约定一致。
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PageQuery(BaseModel):
    """通用分页参数；页码从 1 开始，每页最多 100 条。"""

    model_config = ConfigDict(populate_by_name=True)

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=12, ge=1, le=100, alias="pageSize")
    q: str = Field(default="", max_length=200)


class CatalogQuery(PageQuery):
    """资源目录的关键词、分类、排序与收藏筛选参数。"""

    category: str = ""
    owner: str = ""
    sort: Literal[
        "recommended", "latest", "popular", "rating", "recent_download", "downloads", "favorites"
    ] = "recommended"
    facets: str = Field(default="{}", max_length=8000)
    favorite_ids: str | None = Field(default=None, max_length=16000, alias="favoriteIds")

    def filters(self) -> tuple[dict[str, list[str]], list[str] | None]:
        """解析 JSON 筛选参数并校验容器和元素类型，非法条件返回 422。"""
        try:
            facets = json.loads(self.facets)
            favorites = json.loads(self.favorite_ids) if self.favorite_ids is not None else None
            if not isinstance(facets, dict) or any(
                not isinstance(values, list) or any(not isinstance(v, str) for v in values)
                for values in facets.values()
            ):
                raise ValueError
            if favorites is not None and (
                not isinstance(favorites, list) or any(not isinstance(v, str) for v in favorites)
            ):
                raise ValueError
            return facets, favorites
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_catalog_filters") from None


class FeedbackInput(Payload):
    """用户反馈资料，限制标题、说明和附件名称长度。"""

    type: str = Field(min_length=1, max_length=40)
    module: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=5000)
    contact: str = Field(default="", max_length=200)
    attachment_name: str = Field(default="", max_length=180)


class InvocationInput(Payload):
    """资源调用的提示词、输出格式与目标版本。"""

    prompt: str = Field(min_length=1, max_length=10000)
    format: str = ""
    version: str = ""


class AccessInput(Payload):
    """资源访问申请的用途、范围和期限说明。"""

    purpose: str = ""
    scope: str = ""
    duration: str = ""
    description: str = Field(default="", max_length=5000)


class PublicationInput(Payload):
    """通用资源发布草稿；场景应用使用独立的完整发布模型。"""

    kind: Literal["dataset", "model", "application", "knowledge", "tool"]
    name: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=5000)
    submit: bool = False
