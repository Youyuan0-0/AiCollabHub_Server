from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Subject = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f]+$")]


class SyncModel(BaseModel):
    """统一同步报文的字段别名规则，并拒绝未约定的字段。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class UserChange(SyncModel):
    """集团最小用户资料变化；Mock 状态推送可不携带姓名。"""

    subject: Subject
    status: Literal["active", "disabled", "deleted"]
    display_name: Annotated[str | None, Field(min_length=1, max_length=100)] = Field(
        default=None, alias="displayName"
    )

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        """拒绝空白姓名，不让已验证批次在数据库约束阶段才失败。"""
        if value is not None and not value.strip():
            raise ValueError("displayName cannot be blank")
        return value


class RoleChange(SyncModel):
    """单个身份的平台管理员授权变化，内部能力码须由外部适配器转换。"""

    subject: Subject
    # 这是平台内部能力码，不假定它等于集团的 roleCode。
    role_code: Literal["platform_admin"] = Field(alias="roleCode")
    status: Literal["authorized", "unauthorized"]


class SyncBatch(SyncModel):
    """同一资源类型的有界事件批次，使用源版本处理重试和乱序。"""

    app_id: Literal["platform"] = Field(alias="appId")
    resource_type: Literal["user", "userlinkedrole"] = Field(alias="resType")
    action: Literal["push", "recycle"]
    # 原样保存 IAM syncTime；正式接口的重试保值约定尚待联调确认。
    sync_time: Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)] = Field(
        alias="syncTime"
    )
    data: list[UserChange | RoleChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_items(self) -> SyncBatch:
        """校验批次资源类型和条目类型一致，并拒绝同一批次重复资源。"""
        expected = UserChange if self.resource_type == "user" else RoleChange
        if any(not isinstance(item, expected) for item in self.data):
            raise ValueError("data does not match resType")
        keys = [(item.subject, getattr(item, "role_code", "")) for item in self.data]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate resource in one batch")
        return self


class SyncResult(SyncModel):
    """返回可查询的回执编号，不重复持久化统计或假定存在集团事件编号。"""

    receipt_id: UUID = Field(alias="receiptId")
    result_code: Literal[200] = Field(default=200, alias="resultCode")
    result_msg: str = Field(default="success", alias="resultMsg")
    resource_type: str = Field(alias="resType")
    sync_time: int = Field(alias="syncTime")
    duplicate: bool = False


def request_schema() -> dict:
    """展开批次子模型定义，供自定义流式读取端点生成完整 OpenAPI 请求体。"""
    # 有界流式读取使用自定义依赖；内联子模型让 OpenAPI 请求体仍可直接查看和调用。
    schema = SyncBatch.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value: object) -> object:
        """递归展开本地模型引用，保留其他 JSON Schema 值。"""
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return cast(dict, expand(schema))
