"""集团 IAM 协议边界；未确认签名规则前不得提供默认放行实现。"""

from collections.abc import Mapping
from typing import Literal, Protocol

from app.modules.common.auth.cas import Principal


class CasClient(Protocol):
    """CAS 客户端负责集团规定的验票签名和可信响应校验。"""

    def login_url(self, service: str, renew: bool = False) -> str:
        """浏览器登录地址必须携带预先绑定的原始回调地址。"""
        ...

    def logout_url(self, service: str) -> str:
        """返回集团登出入口，不复用 Mock 域名。"""
        ...

    def validate(self, ticket: str, service: str) -> Principal:
        """验票并返回与身份推送编号对应的可信主体。"""
        ...

    def close(self) -> None:
        """释放客户端连接。"""
        ...


class IamIngressVerifier(Protocol):
    """在处理通知前认证原始请求；appId 和转发头不能单独证明来源。"""

    def verify(
        self,
        purpose: Literal["logout", "sync"],
        body: bytes,
        headers: Mapping[str, str],
    ) -> None:
        """按甲方认可的规则验证签名或来源，失败时必须拒绝。"""
        ...
