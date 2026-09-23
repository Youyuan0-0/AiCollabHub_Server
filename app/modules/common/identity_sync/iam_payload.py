"""将集团 IAM-02 报文缩减为平台最小身份事件，不保存多余的个人资料。"""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.modules.common.identity_sync.schemas import SyncBatch


class IamPayload(BaseModel):
    """只提取已用到的字段；集团报文的其他个人字段不进入数据库。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    app_id: str = Field(alias="appId", min_length=1)
    resource_type: Literal["user", "userlinkedrole"] = Field(alias="resType")
    action: Literal["push", "recycle"]
    sync_time: int = Field(alias="syncTime", gt=0)
    size: int = Field(ge=1, le=100)
    data: list[dict] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_size(self) -> "IamPayload":
        """拒绝数量不符的批次，避免收到部分数据仍误报同步成功。"""
        if self.size != len(self.data):
            raise ValueError("size does not match data length")
        return self


class IamUser(BaseModel):
    """CAS workNo 与用户推送 workNo 对齐，字段缺失时拒绝处理。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    work_no: str = Field(alias="workNo", min_length=1, max_length=200)
    display_name: str | None = Field(default=None, alias="displayName", max_length=100)
    status: str | int | None = None
    sync_status: int | None = Field(default=None, alias="syncStatus")


class IamRole(BaseModel):
    """仅接受本平台已确认的管理员能力码，不推断其他角色的权限。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    user_no: str = Field(alias="userNo", min_length=1, max_length=200)
    role_code: str = Field(alias="roleCode", min_length=1)
    status: Literal["authorized", "unauthorized"] | None = None
    sync_status: int | None = Field(default=None, alias="syncStatus")


def normalize_iam_payload(body: bytes, app_id: str, admin_role_code: str) -> SyncBatch:
    """校验应用、状态及编号后转为共享事务事件；未知权限或状态不确认成功。"""
    try:
        payload = IamPayload.model_validate_json(body)
        if payload.app_id != app_id:
            raise HTTPException(403, "iam_app_not_allowed")
        changes = []
        for item in payload.data:
            if payload.resource_type == "user":
                user = IamUser.model_validate(item)
                if user.sync_status not in (None, 2000, 800):
                    raise ValueError("unsupported syncStatus")
                status = {"2000": "active", "3000": "disabled", "-1000": "deleted"}.get(
                    str(user.status)
                )
                if payload.action == "recycle" or user.sync_status == 800:
                    status = "disabled"
                if status is None:
                    raise ValueError("unsupported user status")
                changes.append(
                    {"subject": user.work_no, "status": status, "displayName": user.display_name}
                )
            else:
                role = IamRole.model_validate(item)
                if role.role_code != admin_role_code:
                    raise ValueError("unmapped roleCode")
                if role.sync_status not in (None, 2000, 800):
                    raise ValueError("unsupported syncStatus")
                status = (
                    "unauthorized"
                    if payload.action == "recycle" or role.sync_status == 800
                    else role.status
                )
                if status is None:
                    raise ValueError("missing role status")
                changes.append(
                    {"subject": role.user_no, "roleCode": "platform_admin", "status": status}
                )
        return SyncBatch.model_validate(
            {
                "appId": "platform",
                "resType": payload.resource_type,
                "action": payload.action,
                "syncTime": payload.sync_time,
                "data": changes,
            }
        )
    except ValidationError as error:
        raise HTTPException(
            422, error.errors(include_input=False, include_context=False, include_url=False)
        ) from None
    except ValueError:
        raise HTTPException(422, "unsupported_iam_payload") from None
