from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Subject = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
EventId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]


class SyncModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class UserChange(SyncModel):
    subject: Subject
    status: Literal["active", "disabled", "deleted"]


class RoleChange(SyncModel):
    subject: Subject
    # 这是平台内部能力码，不假定它等于集团的 roleCode。
    role_code: Literal["platform_admin"] = Field(alias="roleCode")
    status: Literal["authorized", "unauthorized"]


class SyncBatch(SyncModel):
    event_id: EventId = Field(alias="eventId")
    app_id: Literal["platform"] = Field(alias="appId")
    resource_type: Literal["user", "userlinkedrole"] = Field(alias="resType")
    action: Literal["push", "recycle"]
    # 同步源提供的单调版本；模拟器采用毫秒时间戳，服务端不使用到达时间排序。
    sync_time: Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)] = Field(
        alias="syncTime"
    )
    data: list[UserChange | RoleChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_items(self):
        expected = UserChange if self.resource_type == "user" else RoleChange
        if any(not isinstance(item, expected) for item in self.data):
            raise ValueError("data does not match resType")
        keys = [(item.subject, getattr(item, "role_code", "")) for item in self.data]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate resource in one batch")
        return self


class SyncResult(SyncModel):
    event_id: str = Field(alias="eventId")
    resource_type: str = Field(alias="resType")
    sync_time: int = Field(alias="syncTime")
    applied: int
    ignored: int
    unchanged: int
    revoked_sessions: int = Field(alias="revokedSessions")
    duplicate: bool = False


def request_schema() -> dict:
    # 有界流式读取使用自定义依赖；内联子模型让 OpenAPI 请求体仍可直接查看和调用。
    schema = SyncBatch.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return expand(schema)
