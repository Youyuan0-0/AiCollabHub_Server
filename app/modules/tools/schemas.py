"""工具发布、运行与配置契约，复用资源分页、审核和可见范围规则。"""

from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.modules.common.management_schemas import AuditQuery as ToolAuditQuery
from app.modules.common.management_schemas import ListingInput as ToolListing
from app.modules.common.management_schemas import ReviewInput, ReviewQuery, VisibilityInput
from app.modules.common.schema_validation import parse_schema
from app.modules.common.schemas import CatalogQuery, Payload
from app.modules.tools.fields import REQUIRED_FIELDS

__all__ = ["ReviewTool", "ToolAuditQuery", "ToolListing", "ToolVisibility"]


class ToolVisibility(VisibilityInput):
    """工具复用集团、指定用户、单位和项目组可见范围及目标校验。"""


class ToolCatalogQuery(CatalogQuery):
    """工具目录额外支持当前账号收藏筛选。"""

    favorites_only: bool = Field(default=False, alias="favoritesOnly")


class ToolReviewQuery(ReviewQuery):
    """复用日期查询，按提交时间或审核优先级排序。"""

    status: Literal["", "draft", "pending", "approved", "rejected", "needs_changes"] = ""
    sort: Literal["updated", "submitted", "priority"] = "submitted"


class ReviewTool(ReviewInput):
    """工具审核增加可选报告，复用七维评分和结论约束。"""

    report_file_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("report_file_ids")
    @classmethod
    def unique_reports(cls, values: list[str]) -> list[str]:
        """拒绝重复报告；归属与修订号由事务内校验。"""
        if len(set(values)) != len(values) or any(not value or len(value) > 80 for value in values):
            raise ValueError("报告附件编号无效或重复")
        return values


class ToolPriority(Payload):
    """审核员调整待审核申请优先级，不增加发布者必填字段。"""

    revision: int = Field(ge=1, strict=True)
    priority: Literal["normal", "high", "urgent"]


class ToolInput(Payload):
    """用户填写的工具资料；身份、编号、统计和上架状态只能由服务端生成。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=200)
    version: str = Field(default="v1.0", max_length=80)
    description: str = Field(default="", max_length=2000)
    description_markdown: str = Field(default="", max_length=30000)
    tool_type: str = Field(default="", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_type: str = Field(default="", max_length=100)
    team: str = Field(default="", max_length=200)
    technical_stack: str = Field(default="", max_length=500)
    environment: str = Field(default="", max_length=1000)
    deployment: str = Field(default="", max_length=100)
    input_schema: str = Field(default="", max_length=30000)
    output_schema: str = Field(default="", max_length=30000)
    license: str = Field(default="", max_length=1000)
    data_level: str = Field(default="", max_length=50)
    endpoint: str = Field(default="", max_length=2000)
    usage_example: str = Field(default="", max_length=10000)
    input_example: str = Field(default="", max_length=10000)
    output_example: str = Field(default="", max_length=10000)


class SaveTool(Payload):
    """草稿允许字段不完整；提交时校验完整资料和两份 JSON Schema。"""

    model_config = ConfigDict(extra="forbid")
    data: ToolInput
    submit: bool = False
    revision: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_submission(self) -> Self:
        """必填规则在后端生效，API 地址保持选填且从不由模拟器访问。"""
        if self.submit:
            data = self.data.model_dump(by_alias=True)
            if any(not data[key] for key in REQUIRED_FIELDS):
                raise ValueError("请补齐工具发布必填项")
            if any(not tag.strip() or len(tag) > 100 for tag in self.data.tags):
                raise ValueError("功能标签不能为空且不能超过 100 字")
            for value in (self.data.input_schema, self.data.output_schema):
                parse_schema(value)
        return self


class InvokeTool(Payload):
    """接受完整 JSON Schema 描述的 JSON 值，不把输入限定为对象。"""

    inputs: object = Field(default_factory=dict)
    simulate_failure: bool = False
