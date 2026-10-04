from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import Settings


class TicketRejected(Exception):
    """统一认证服务已明确拒绝本次票据。"""

    pass


class IAMUnavailable(Exception):
    """统一认证服务不可达或响应无法验证。"""

    def __init__(self, reason: str = "invalid_response") -> None:
        """只保留内部固定原因码，不能传入供应方原始异常或响应。"""
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Principal:
    """经过票据验证的账号标识与显示名称。"""

    subject: str
    display_name: str


class MockCasClient:
    """沿用文档 CAS 路径和报文；模拟密钥只用于本地联调，不冒充集团签名。"""

    def __init__(self, settings: Settings) -> None:
        self.base = str(settings.auth_mock_iam_url).rstrip("/") + "/authn-api/v5/cas/platform"
        internal_origin = settings.auth_mock_iam_internal_url or settings.auth_mock_iam_url
        self.validation_base = str(internal_origin).rstrip("/") + "/authn-api/v5/cas/platform"
        self.client = httpx.Client(
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            headers={"X-Mock-IAM-Key": settings.auth_mock_secret.get_secret_value()},
        )

    def close(self) -> None:
        """关闭模拟认证客户端并释放 HTTP 连接。"""
        self.client.close()

    def login_url(self, service: str, renew: bool = False) -> str:
        """生成包含回调地址与重新认证标记的 CAS 登录地址。"""
        return self.base + "/login?" + urlencode({"service": service, "renew": str(renew).lower()})

    def logout_url(self, service: str) -> str:
        """生成注销后返回指定业务地址的 CAS 注销链接。"""
        return self.base + "/logout?" + urlencode({"service": service})

    def validate(self, ticket: str, service: str) -> Principal:
        """向模拟 IAM 验证票据，检查响应结构后返回可信账号。"""
        try:
            response = self.client.get(
                self.validation_base + "/p3/serviceValidate",
                params={"ticket": ticket, "service": service, "format": "json"},
            )
            response.raise_for_status()
            if len(response.content) > 65536:
                raise IAMUnavailable()
            data = response.json()["serviceResponse"]
            if "authenticationFailure" in data:
                raise TicketRejected()
            success = data["authenticationSuccess"]
            if not isinstance(success, dict) or not isinstance(success.get("attributes", {}), dict):
                raise IAMUnavailable()
            subject = success["user"]
            name = success.get("attributes", {}).get("displayName", subject)
            if not isinstance(subject, str) or not subject or len(subject) > 200:
                raise IAMUnavailable()
            if not isinstance(name, str) or not name or len(name) > 100:
                raise IAMUnavailable()
            return Principal(subject, name)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise IAMUnavailable() from None
