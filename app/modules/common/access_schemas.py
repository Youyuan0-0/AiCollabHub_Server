"""资源授权的公共目标结构，具体模块自行限定可配置范围与修订规则。"""

from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from app.modules.common.schemas import Payload


class StrictPayload(Payload):
    """拒绝未声明字段并清理文本空白，保持平台 JSON 字段约定。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScopeTargets(StrictPayload):
    """兼容历史成员权限的读取结构；新业务可收窄 scope 枚举。"""

    scope: Literal["all", "organizations", "members", "projects"] = "all"
    targets: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("targets")
    @classmethod
    def clean_targets(cls, values: list[str]) -> list[str]:
        """清理授权目标并拒绝空标识，避免通过显示名称推断权限。"""
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("请填写有效的组织、成员或项目组标识")
        return list(dict.fromkeys(value.strip() for value in values))
