from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import Settings


class TicketRejected(Exception):
    pass


class IAMUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    display_name: str


class MockCasClient:
    """沿用文档 CAS 路径和报文；模拟密钥只用于本地联调，不冒充集团签名。"""

    def __init__(self, settings: Settings):
        self.base = str(settings.auth_mock_iam_url).rstrip("/") + "/authn-api/v5/cas/platform"
        self.client = httpx.Client(
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            headers={"X-Mock-IAM-Key": settings.auth_mock_secret.get_secret_value()},
        )

    def close(self):
        self.client.close()

    def login_url(self, service: str, renew: bool = False) -> str:
        return self.base + "/login?" + urlencode({"service": service, "renew": str(renew).lower()})

    def logout_url(self, service: str) -> str:
        return self.base + "/logout?" + urlencode({"service": service})

    def validate(self, ticket: str, service: str) -> Principal:
        try:
            response = self.client.get(
                self.base + "/p3/serviceValidate",
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
