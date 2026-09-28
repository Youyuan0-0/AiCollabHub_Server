"""模型后台接入公共审核流程，保留模型专属完整性检查。"""

from typing import Literal

from fastapi import Request
from pydantic import Field

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.identity import current_actor
from app.modules.common.resource_admin import resource_admin_router
from app.modules.models.schemas import MODEL_FIELDS, MODEL_REQUIRED, ModelInput, validate_model
from app.modules.models.workspace import model_workspace


def validate(data: dict) -> None:
    """仅把模型业务字段送入严格契约，自动生成字段不作为用户输入。"""
    validate_model(
        ModelInput(
            **{
                key: value
                for key, value in data.items()
                if key in {field.alias for field in ModelInput.model_fields.values()}
            }
        )
    )


router = resource_admin_router(
    "model",
    "models",
    model_workspace,
    {
        **MODEL_FIELDS,
        "id": "模型 ID",
        "createdAt": "创建时间",
        "updatedAt": "更新时间",
        "downloadCount": "下载量",
        "favoriteCount": "收藏数",
        "qualityScore": "质量评分",
        "rating": "用户评分",
    },
    MODEL_REQUIRED,
    validate,
)


class ModelMetadata(StrictPayload):
    """模型来源、关联训练集和安全等级属于后台管理资料。"""

    revision: int = Field(ge=1)
    source_type: Literal["自研", "微调", "第三方引入"] = "自研"
    training_dataset_ids: list[str] = Field(default_factory=list, max_length=100)
    security_level: str = Field(default="内部", min_length=1, max_length=100)


@router.put("/model-publications/{identifier}/metadata")
def metadata(identifier: str, payload: ModelMetadata, request: Request) -> dict:
    """仅修改经过模型管理契约验证的字段。"""
    return model_workspace(request).update_metadata(
        identifier,
        payload.model_dump(by_alias=True, exclude={"revision"}),
        payload.revision,
        current_actor(request, True),
    )
