"""按集团确认的无签名 CAS 协议验票，始终以 workNo 关联本地用户。"""

import json
from urllib.parse import urlencode

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.core.config import Settings
from app.modules.common.auth.cas import IAMUnavailable, Principal, TicketRejected

CAS_NAMESPACE = "{http://www.yale.edu/tp/cas}"
MAX_RESPONSE_BYTES = 65536


def _principal(work_no: object, display_name: object) -> Principal:
    """只接受明确的集团用户编号，不使用登录名或角色字段推断用户身份。"""
    if (
        not isinstance(work_no, str)
        or not work_no
        or work_no != work_no.strip()
        or len(work_no) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in work_no)
    ):
        raise IAMUnavailable()
    if display_name is None or display_name == "":
        display_name = work_no
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name) > 100
        or any(ord(char) < 32 or ord(char) == 127 for char in display_name)
    ):
        raise IAMUnavailable()
    return Principal(work_no, display_name)


def _json_principal(body: bytes) -> Principal:
    """解析文档 JSON 响应，明确区分票据拒绝与无法识别的服务响应。"""
    envelope = json.loads(body)
    if not isinstance(envelope, dict):
        raise IAMUnavailable()
    response = envelope.get("serviceResponse")
    if not isinstance(response, dict):
        raise IAMUnavailable()
    if "authenticationFailure" in response:
        if "authenticationSuccess" in response:
            raise IAMUnavailable()
        failure = response["authenticationFailure"]
        if not isinstance(failure, dict) or not failure.get("code"):
            raise IAMUnavailable()
        raise TicketRejected()
    success = response.get("authenticationSuccess")
    if not isinstance(success, dict) or not isinstance(success.get("attributes"), dict):
        raise IAMUnavailable()
    attributes = success["attributes"]
    return _principal(attributes.get("workNo"), attributes.get("displayName"))


def _xml_principal(body: bytes) -> Principal:
    """兼容 CAS 默认 XML 格式，禁用 DTD 和实体展开并拒绝重复身份字段。"""
    root = ElementTree.fromstring(body, forbid_dtd=True)
    if root.tag != CAS_NAMESPACE + "serviceResponse":
        raise IAMUnavailable()
    failures = root.findall(CAS_NAMESPACE + "authenticationFailure")
    successes = root.findall(CAS_NAMESPACE + "authenticationSuccess")
    if failures:
        if len(failures) != 1 or successes or not failures[0].get("code"):
            raise IAMUnavailable()
        raise TicketRejected()
    if len(successes) != 1:
        raise IAMUnavailable()
    attributes = successes[0].findall(CAS_NAMESPACE + "attributes")
    if len(attributes) != 1:
        raise IAMUnavailable()
    work_numbers = attributes[0].findall(CAS_NAMESPACE + "workNo")
    names = attributes[0].findall(CAS_NAMESPACE + "displayName")
    if len(work_numbers) != 1 or len(names) > 1:
        raise IAMUnavailable()
    return _principal(work_numbers[0].text, names[0].text if names else None)


class IamCasClient:
    """集团 CAS 客户端；不发送 Mock 密钥，不重试一次性票据，不跟随验票重定向。"""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.auth_iam_origin or not settings.auth_iam_app_id:
            raise ValueError("IAM origin and app ID are required")
        self.base = (
            str(settings.auth_iam_origin).rstrip("/")
            + "/authn-api/v5/cas/"
            + settings.auth_iam_app_id
        )
        self.client = httpx.Client(
            timeout=settings.auth_iam_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def login_url(self, service: str, renew: bool = False) -> str:
        """回调参数编码一次；只在显式切换账号时发送 CAS renew 参数。"""
        params = {"service": service}
        if renew:
            params["renew"] = "true"
        return self.base + "/login?" + urlencode(params)

    def logout_url(self, service: str) -> str:
        """生成主动退出 IAM 的地址，不代表集团提供单点登出通知。"""
        return self.base + "/logout?" + urlencode({"service": service})

    def validate(self, ticket: str, service: str) -> Principal:
        """用原始 service 验证票据，限制响应体大小后读取 workNo 和姓名。"""
        try:
            with self.client.stream(
                "GET",
                self.base + "/p3/serviceValidate",
                params={"ticket": ticket, "service": service, "format": "json"},
            ) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise IAMUnavailable()
            content = bytes(body)
            if content.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
                return _xml_principal(content)
            return _json_principal(content)
        except (
            httpx.HTTPError,
            ValueError,
            TypeError,
            ElementTree.ParseError,
            DefusedXmlException,
        ):
            # 外部异常通常包含带 ticket 的请求 URL，不向日志或调用方传播原文。
            raise IAMUnavailable() from None

    def close(self) -> None:
        """释放验票使用的 HTTP 连接池。"""
        self.client.close()
