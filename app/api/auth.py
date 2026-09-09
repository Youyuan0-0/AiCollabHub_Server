from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.core.auth import IdentityProvider, Principal, PrincipalDependency

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginUrlResponse(BaseModel):
    url: str


@router.get("/session", response_model=Principal)
async def session(principal: PrincipalDependency) -> Principal:
    return principal


@router.get("/login-url", response_model=LoginUrlResponse)
async def login_url(request: Request, return_to: str = Query("/", alias="returnTo")):
    # 回跳仅接受站内路径，避免由登录接口形成开放重定向。
    if (
        not return_to.startswith("/")
        or return_to.startswith("//")
        or any(character in return_to for character in ("\\", "\r", "\n"))
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="invalid_return_path")
    provider: IdentityProvider | None = request.app.state.identity_provider
    if provider is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="sso_not_configured")
    url = await provider.login_url(return_to)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="invalid_login_url")
    return LoginUrlResponse(url=url)
