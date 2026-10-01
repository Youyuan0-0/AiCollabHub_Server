"""数据集用户端发布契约；系统标识、审核状态和开放范围不由表单填写。"""

from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import ConfigDict, Field, ValidationInfo, field_validator

from app.modules.common.interaction_schemas import FavoriteInput as FavoriteDataset
from app.modules.common.interaction_schemas import RatingInput as RateDataset
from app.modules.common.schema_validation import parse_schema
from app.modules.common.schemas import Payload
from app.modules.datasets.classification import classification_label

Label = Annotated[str, Field(min_length=1, max_length=100)]
# 排行榜仅接受平台实际维护的两个累计指标，不允许客户端指定任意排序字段。
DatasetRankingMetric = Literal["downloads", "favorites"]


class DatasetInput(Payload):
    """保存数据集资料；草稿允许缺项，提交时再检查业务必填和字段 Schema。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=200)
    english_name: str = Field(default="", max_length=200)
    version: str = Field(default="v1.0", max_length=80)
    summary: str = Field(default="", max_length=1000)
    description_markdown: str = Field(default="", max_length=30000)
    owner: str = Field(default="", max_length=200)
    contact: str = Field(default="", max_length=200)
    tags: list[Label] = Field(default_factory=list, max_length=20)
    tasks: list[Label] = Field(default_factory=list, max_length=20)
    data_application: list[Label] = Field(default_factory=list, max_length=20)
    data_shape: list[Label] = Field(default_factory=list, max_length=20)
    ai_purpose: str = Field(default="", max_length=100)
    field_schema: str = Field(default="", max_length=30000)
    language: list[Label] = Field(default_factory=list, max_length=20)
    data_format: list[Label] = Field(default_factory=list, max_length=20)
    data_level: Literal["", "核心数据", "重要数据", "一般数据"] = ""
    security_level: Literal["public", "internal"] = "internal"
    time_coverage: str = Field(default="", max_length=300)
    spatial_coverage: str = Field(default="", max_length=300)
    update_frequency: str = Field(default="", max_length=100)
    record_count: int | None = Field(default=None, ge=0)
    data_size: str = Field(default="", max_length=100)
    sample_data: str = Field(default="", max_length=20000)
    version_notes: str = Field(default="", max_length=2000)

    @field_validator("tags", "tasks", "data_application", "data_shape", "language", "data_format")
    @classmethod
    def unique_labels(cls, values: list[str], info: ValidationInfo) -> list[str]:
        """去除重复标签和首尾空白，拒绝空白标签以避免不可见的筛选项。"""
        if any(not value.strip() for value in values):
            raise ValueError("标签不能只包含空白")
        field = {"data_application": "dataApplication", "data_shape": "dataShape"}.get(
            info.field_name, ""
        )
        return list(dict.fromkeys(classification_label(field, value.strip()) for value in values))

    @field_validator("ai_purpose")
    @classmethod
    def normalize_purpose(cls, value: str) -> str:
        """兼容历史用途名称，微调归入后训练，知识库历史用途不强制改写。"""
        return classification_label("aiPurpose", value)

    @field_validator("security_level", mode="before")
    @classmethod
    def normalize_security(cls, value: object) -> object:
        """密级只接收公开和内部，兼容中文输入但拒绝涉密级别。"""
        return (
            {"公开": "public", "内部": "internal"}.get(value, value)
            if isinstance(value, str)
            else value
        )


class SaveDataset(Payload):
    """创建或更新发布申请；revision 用于阻止旧页面覆盖已保存内容。"""

    model_config = ConfigDict(extra="forbid")
    data: DatasetInput
    submit: bool = False
    revision: int | None = Field(default=None, ge=1)


def validate_submission(data: DatasetInput) -> None:
    """检查发布必填项和 Schema；失败返回中文 422，草稿保存不调用此检查。"""
    required = {
        "名称": data.name,
        "版本号": data.version,
        "简介": data.summary,
        "详细描述": data.description_markdown,
        "所属单位": data.owner,
        "联系人": data.contact,
        "数据应用": data.data_application,
        "数据形态": data.data_shape,
        "AI应用目的": data.ai_purpose,
        "字段清单": data.field_schema,
        "数据语言": data.language,
        "数据格式": data.data_format,
        "数据分级": data.data_level,
    }
    missing = [label for label, value in required.items() if not value]
    # 规模可按记录数或数据量描述，0 是有效记录数；草稿仍允许两项均未填写。
    if data.record_count is None and not data.data_size:
        missing.append("数据规模（记录数或数据量至少填写一项）")
    if missing:
        raise HTTPException(422, "请填写：" + "、".join(missing))
    try:
        schema, _ = parse_schema(data.field_schema)
        if schema.get("type") != "object" or not schema.get("properties"):
            raise ValueError("字段清单需为包含 properties 字段的 object 类型 JSON Schema")
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


__all__ = ["DatasetInput", "SaveDataset", "FavoriteDataset", "RateDataset", "validate_submission"]
