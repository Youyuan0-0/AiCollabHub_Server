"""场景应用演示接口的数据约定；不是数据库表模型。"""

import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from app.modules.common.schemas import Payload

APPLICATION_CATEGORIES = ("文档创作", "会议办公", "数据处理", "翻译沟通", "法律合规")


# 沿用公共 Payload 的字段别名规则，并拒绝未定义字段、清理字符串首尾空白。
class ApplicationPayload(Payload):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tutorial(ApplicationPayload):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal["document", "video"] = "document"
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str):
        if "\\" in value or any(ord(char) < 32 for char in value):
            raise ValueError("教程链接格式不正确")
        parsed = urlsplit(value)
        if value.startswith("/") and not value.startswith("//"):
            return value
        if parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username:
            return value
        raise ValueError("请填写 http(s) 链接或站内路径")


class ConfigurationOption(ApplicationPayload):
    name: str = Field(min_length=1, max_length=80)
    value: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=500)


class ApplicationDependency(ApplicationPayload):
    kind: Literal["model", "dataset", "knowledge", "tool"]
    resource_id: str = Field(min_length=1, max_length=200)


# 请求正文 data 对应的应用资料；Field 和校验函数在路由处理前检查长度与格式。
class ApplicationDetails(ApplicationPayload):
    name: str = Field(default="", max_length=200)
    english_name: str = Field(default="", max_length=200)
    version: str = Field(default="v1.0", max_length=80)
    summary: str = Field(default="", max_length=1000)
    description_markdown: str = Field(default="", max_length=30000)
    category: str = Field(default="文档创作", max_length=80)
    owner: str = Field(default="", max_length=200)
    contact: str = Field(default="", max_length=200)
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
    def schema_document(cls, value: str):
        if not value:
            return value

        def invalid_constant(_value):
            raise ValueError("JSON 不支持 NaN 或 Infinity")

        try:
            # 只校验资料格式；不会据此执行协议或远程解析引用。
            parsed = json.loads(value, parse_constant=invalid_constant)
        except (ValueError, RecursionError) as error:
            raise ValueError("输入/输出格式需要填写有效的 JSON 对象，或暂时留空") from error
        if not isinstance(parsed, dict):
            raise ValueError("输入/输出格式需要填写 JSON 对象")
        return value

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, values: list[ApplicationDependency]):
        keys = [(item.kind, item.resource_id) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("同一关联资源不能重复添加")
        return values

    @field_validator("tags", "capabilities")
    @classmethod
    def clean_labels(cls, values: list[str]):
        labels = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 80 for value in labels):
            raise ValueError("单个标签不能超过 80 字")
        return labels


class QualityScores(ApplicationPayload):
    completeness: int = Field(ge=1, le=5)
    usability: int = Field(ge=1, le=5)
    security: int = Field(ge=1, le=5)


# 前端保存申请的 JSON 在这里解析；字段格式不合规则由 FastAPI 返回 422。
class SaveApplicationPublication(ApplicationPayload):
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
    scores: QualityScores | None = None
    decision: Literal["approved", "rejected", "needs_changes"]
    reason: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1)


class ApplicationVisibility(ApplicationPayload):
    revision: int | None = Field(default=None, ge=1)
    scope: Literal["all", "organizations", "members", "projects"] = "all"
    targets: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("targets")
    @classmethod
    def clean_targets(cls, values: list[str]):
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("请填写有效的组织或成员标识")
        return list(dict.fromkeys(value.strip() for value in values))
