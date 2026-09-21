"""工具链 HTTP 连接复用证书和代理处理，各服务独立配置取值。"""

import ssl


def http_options(proxy_url: object = None, ca_certificate: str = "") -> dict:
    """使用系统信任库并可补充现场 CA，始终保留服务端证书校验。"""
    context = ssl.create_default_context()
    if ca_certificate:
        context.load_verify_locations(cadata=ca_certificate)
    return {"proxy": str(proxy_url) if proxy_url else None, "verify": context}
