from collections.abc import Callable
from typing import Annotated, Protocol

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Principal(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(min_length=1)
    display_name: str
    permissions: list[str] = Field(default_factory=list)


# 协议由后续 SSO 适配器实现；只有服务端验证过的身份才能转换为 Principal。
class IdentityProvider(Protocol):
    async def authenticate(self, request: Request) -> Principal | None: ...

    async def login_url(self, return_to: str) -> str: ...


async def current_principal(request: Request) -> Principal:
    provider: IdentityProvider | None = request.app.state.identity_provider
    principal = await provider.authenticate(request) if provider is not None else None
    if principal is None:
        # 不信任 X-User、X-Role 或未经验证的 Bearer 值，未接 SSO 时默认拒绝。
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="authentication_required")
    return principal


PrincipalDependency = Annotated[Principal, Depends(current_principal)]


def require_permissions(*permissions: str) -> Callable[..., Principal]:
    def authorize(principal: PrincipalDependency) -> Principal:
        if not set(permissions).issubset(principal.permissions):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="permission_denied")
        return principal

    return authorize
