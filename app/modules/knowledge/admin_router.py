"""知识库后台专属字段与审核完整性规则。"""

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import Field

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.identity import current_actor
from app.modules.common.resource_admin import resource_admin_router
from app.modules.knowledge.schemas import IndexInput
from app.modules.knowledge.workspace import knowledge_workspace

KNOWLEDGE_FIELDS = {
    "name": "知识库名称",
    "summary": "知识库简介",
    "description": "知识库描述",
    "indexConfig": "索引配置",
    "dataLevel": "数据分级",
    "owner": "所属单位",
    "contact": "联系人",
    "documentCount": "文档数量",
    "totalSize": "总容量（字节）",
    "uploadedAt": "上传时间",
    "indexBuiltAt": "索引构建时间",
    "averageRetrievalMs": "平均召回耗时（毫秒）",
    "retrievalSuccessRate": "召回成功率（%）",
}
KNOWLEDGE_REQUIRED = ("name", "summary", "indexConfig", "dataLevel")


def validate(data: dict) -> None:
    """分级仅由管理员填写，缺少时不得审批通过。"""
    if any(not data.get(key) for key in KNOWLEDGE_REQUIRED):
        raise HTTPException(422, "请补齐知识库名称、简介、索引配置和后台数据分级")
    if data["dataLevel"] not in ("核心数据", "重要数据", "一般数据"):
        raise HTTPException(422, "请选择知识库数据分级")
    IndexInput(**data["indexConfig"])


router = resource_admin_router(
    "knowledge", "knowledge", knowledge_workspace, KNOWLEDGE_FIELDS, KNOWLEDGE_REQUIRED, validate
)


class KnowledgeMetadata(StrictPayload):
    """知识库分级和维护资料仅在管理端填写。"""

    revision: int = Field(ge=1)
    data_level: Literal["核心数据", "重要数据", "一般数据"]
    owner: str = Field(min_length=1, max_length=200)
    contact: str = Field(min_length=1, max_length=200)


@router.put("/knowledge-publications/{identifier}/metadata")
def metadata(identifier: str, payload: KnowledgeMetadata, request: Request) -> dict:
    """维护知识库管理员专属字段并更新申请修订。"""
    return knowledge_workspace(request).update_metadata(
        identifier,
        payload.model_dump(by_alias=True, exclude={"revision"}),
        payload.revision,
        current_actor(request, True),
    )
