"""从平台认证依赖解析反馈操作身份。"""

from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class FeedbackActor:
    """由服务端校验得到的操作身份。"""

    id: str
    name: str


def actor(request: Request, *, admin: bool = False) -> FeedbackActor:
    """复用业务鉴权结果；未启用登录时仅允许开发环境固定身份。"""
    identity = getattr(request.state, "business_identity", None)
    if identity:
        if admin and identity["scope"] != "admin":
            raise HTTPException(403, "没有处理反馈的权限")
        return FeedbackActor(identity["id"], identity["name"][:100])
    settings = request.app.state.settings
    if settings.auth_mode == "disabled" and settings.app_env != "production":
        return (
            FeedbackActor("demo-admin", "演示管理员")
            if admin
            else FeedbackActor("demo-user", "演示用户")
        )
    raise HTTPException(401, "authentication_required")
