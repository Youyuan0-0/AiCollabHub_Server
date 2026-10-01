"""集团与模拟 CAS 共用的客户端契约。"""

from typing import Protocol

from app.modules.common.auth.cas import Principal


class CasClient(Protocol):
    """CAS 客户端负责服务端验票、响应校验及登录和退出地址生成。"""

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
