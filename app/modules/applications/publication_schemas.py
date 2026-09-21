"""场景应用演示接口的数据约定；不是数据库表模型。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from app.modules.common.review_schemas import ReviewQualityScores
from app.modules.common.schema_validation import parse_schema
from app.modules.common.schemas import Payload

APPLICATION_CATEGORIES = ("文档创作", "会议办公", "数据处理", "翻译沟通", "法律合规")


# 沿用公共 Payload 的字段别名规则，并拒绝未定义字段、清理字符串首尾空白。
class ApplicationPayload(Payload):
    """应用请求模型基类，统一字段别名、空白清理和未知字段拒绝规则。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TutorialFile(ApplicationPayload):
    """教程附件的公开元信息；保存时按附件编号从后端重新核对。"""

    id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    name: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=100 * 1024 * 1024)
    content_type: str = Field(min_length=1, max_length=128)
    security_level: Literal["internal", "public"]


class Tutorial(ApplicationPayload):
    """应用教程资料，兼容原有链接，并支持上传的文档或视频。"""

    title: str = Field(min_length=1, max_length=120)
    kind: Literal["document", "video"] = "document"
    url: str = Field(default="", max_length=2000)
    source: Literal["link", "file"] = "link"
    attachment: TutorialFile | None = None

    @model_validator(mode="after")
    def valid_source(self) -> Tutorial:
        """链接和附件二选一，未完成的上传不能保存为教程。"""
        if self.source == "file":
            if self.attachment is None or self.url:
                raise ValueError("请上传教程文件，或切换为填写链接")
        elif not self.url or self.attachment is not None:
            raise ValueError("请填写教程链接")
        return self

    @model_serializer(mode="wrap")
    def serialize_tutorial(self, handler: SerializerFunctionWrapHandler) -> dict:
        """保留旧链接教程的响应形状，附件教程输出新增字段。"""
        data = handler(self)
        if self.source == "link":
            data.pop("source", None)
            data.pop("attachment", None)
        return data

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        """允许无凭据的 HTTP(S) 链接或站内路径，拒绝反斜线和控制字符。"""
        if not value:
            return value
        if "\\" in value or any(ord(char) < 32 for char in value):
            raise ValueError("教程链接格式不正确")
        parsed = urlsplit(value)
        if value.startswith("/") and not value.startswith("//"):
            return value
        if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username:
            return value
        raise ValueError("请填写 http(s) 链接或站内路径")


class ConfigurationOption(ApplicationPayload):
    """应用运行配置项的名称、默认值和说明。"""

    name: str = Field(min_length=1, max_length=80)
    value: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=500)


class ApplicationDependency(ApplicationPayload):
    """关联资源的业务类型和平台资源编号。"""

    kind: Literal["model", "dataset", "knowledge", "tool"]
    resource_id: str = Field(min_length=1, max_length=200)


# 请求正文 data 对应的应用资料；Field 和校验函数在路由处理前检查长度与格式。
class ApplicationDetails(ApplicationPayload):
    """应用发布资料，统一限制文本长度和关联资源数量。"""

    name: str = Field(default="", max_length=200)
    english_name: str = Field(default="", max_length=200)
    version: str = Field(default="v1.0", max_length=80)
    summary: str = Field(default="", max_length=1000)
    description_markdown: str = Field(default="", max_length=30000)
    category: str = Field(default="文档创作", max_length=80)
    owner: str = Field(default="", max_length=200)
    contact: str = Field(default="", max_length=200)
    development_team: str = Field(default="", max_length=300)
    sharing_agreement: str = Field(default="", max_length=5000)
    tool_list: list[str] = Field(default_factory=list, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=20)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    base_model: str = Field(default="", max_length=200)
    tutorials: list[Tutorial] = Field(default_factory=list, max_length=10)
    config_options: list[ConfigurationOption] = Field(default_factory=list, max_length=20)
    usage_limits: str = Field(default="", max_length=5000)
    technical_stack: str = Field(default="", max_length=500)
    deployment: str = Field(default="", max_length=200)
    trigger_type: str = Field(default="", max_length=200)
    communication_protocol: str = Field(default="", max_length=200)
    input_schema: str = Field(default="", max_length=20000)
    output_schema: str = Field(default="", max_length=20000)
    dependencies: list[ApplicationDependency] = Field(default_factory=list, max_length=40)
    version_notes: str = Field(default="", max_length=5000)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schema_document(cls, value: str) -> str:
        """校验可选的 JSON Schema 文档，不编译远程引用。"""
        if not value:
            return value
        parse_schema(value)
        return value

    @field_validator("tool_list")
    @classmethod
    def clean_tools(cls, values: list[str]) -> list[str]:
        """清理工具说明中的空白和重复条目，并限制单项长度。"""
        tools = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 200 for value in tools):
            raise ValueError("单项工具说明不能超过 200 字")
        return tools

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(
        cls, values: list[ApplicationDependency]
    ) -> list[ApplicationDependency]:
        """按资源类型与编号拒绝重复关联。"""
        keys = [(item.kind, item.resource_id) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("同一关联资源不能重复添加")
        return values

    @field_validator("tags", "capabilities")
    @classmethod
    def clean_labels(cls, values: list[str]) -> list[str]:
        """清理标签中的空白和重复条目，并限制单个标签长度。"""
        labels = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 80 for value in labels):
            raise ValueError("单个标签不能超过 80 字")
        return labels


class QualityScores(ReviewQualityScores):
    """应用审核的七项质量评分，每项为 1 至 5 分整数，不设置及格线。"""

    model_config = ConfigDict(str_strip_whitespace=True)


# 前端保存申请的 JSON 在这里解析；字段格式不合规则由 FastAPI 返回 422。
class SaveApplicationPublication(ApplicationPayload):
    """保存草稿或提交审核的请求，更新已有申请时携带 revision。"""

    auto_classify: bool = False
    security_level: Literal["internal", "public", "confidential"] = "internal"
    priority: Literal["normal", "high", "urgent"] = "normal"
    # 前端 data：本次填写的应用资料。
    data: ApplicationDetails
    # 前端 submit：False 保存草稿，True 提交审核。
    submit: bool = False
    # 修改时携带上次读取的修订号；一致性检查由 ApplicationService 执行。
    revision: int | None = Field(default=None, ge=1)


# 审核正文：申请编号在 URL 中，审核结论、原因及修订号在此对象中。
class ReviewApplicationPublication(ApplicationPayload):
    """审核决定、理由、评分和申请版本，由业务服务执行状态校验。"""

    scores: QualityScores
    decision: Literal["approved", "rejected", "needs_changes"]
    reason: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1)
    report_file_ids: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("report_file_ids")
    @classmethod
    def unique_report_files(cls, values: list[str]) -> list[str]:
        """拒绝重复或无效报告编号，归属由服务层校验。"""
        if len(set(values)) != len(values) or any(not item or len(item) > 80 for item in values):
            raise ValueError("报告附件编号无效或重复")
        return values


class ApplicationVisibility(ApplicationPayload):
    """应用开放范围及其版本，目标编号由后端进一步检查权限。"""

    revision: int | None = Field(default=None, ge=1)
    scope: Literal["all", "organizations", "members", "projects"] = "all"
    targets: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("targets")
    @classmethod
    def clean_targets(cls, values: list[str]) -> list[str]:
        """清理开放目标编号的空白和重复项，拒绝空白或过长编号。"""
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("请填写有效的组织或成员标识")
        return list(dict.fromkeys(value.strip() for value in values))
