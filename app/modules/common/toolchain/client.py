"""共用 POP ROA 签名和受控 HTTP 调用；接口字段与分页由业务适配器处理。"""

import json
import logging
from threading import BoundedSemaphore
from time import perf_counter
from uuid import uuid4

import httpx
from aliyunsdkcore.request import CommonRequest
from fastapi import HTTPException

from app.core.config import Settings
from app.modules.common.toolchain.network import http_options

logger = logging.getLogger(__name__)


class PopClient:
    """通过官方 SDK 签名、httpx 传输，模拟与真实环境使用同一套请求代码。"""

    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        *,
        api_version: str = "2025-11-10",
        pop_code: str = "sfm",
    ) -> None:
        self.settings = settings
        self.mode = settings.toolchain_mode
        self.api_version = api_version
        self.pop_code = pop_code
        self.workspace = settings.toolchain_workspace or (
            "mock-workspace" if pop_code == "sfm" else ""
        )
        if self.mode == "live" and not all(
            (
                settings.toolchain_pop_url,
                settings.toolchain_workspace if pop_code == "sfm" else settings.toolchain_region,
                settings.toolchain_access_key_id,
                settings.toolchain_access_key_secret,
            )
        ):
            raise ValueError("真实工具链模式需要 POP 地址、地域或工作空间及 AccessKey 配置")
        if self.mode == "mock" and transport is None:
            raise ValueError("模拟工具链必须提供本地传输，不能回退到网络")
        self.http = httpx.Client(
            base_url=str(settings.toolchain_pop_url or "https://toolchain.invalid").rstrip("/"),
            timeout=settings.toolchain_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            **http_options(
                settings.toolchain_proxy_url if transport is None else None,
                settings.toolchain_ca_certificate,
            ),
        )
        self._limiter = BoundedSemaphore(8)

    def close(self) -> None:
        """释放实例持有的连接池。"""
        self.http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        action: str,
        query: dict | None = None,
        body: dict | None = None,
    ) -> object:
        """校验业务成功并返回 data；写操作不自动重试，超时明确提示核对结果。"""
        if self.mode == "disabled":
            raise HTTPException(503, "工具链尚未配置")
        allowed_paths = ("/model/",) if self.pop_code == "aipaas" else (
            "/gatectl/", "/knowledge-engine/"
        )
        if not path.startswith(allowed_paths):
            raise ValueError("Unsupported toolchain API path")
        if not self._limiter.acquire(blocking=False):
            raise HTTPException(429, "工具链请求较多，请稍后重试")
        request_id, started = uuid4().hex, perf_counter()
        outcome = "failed"
        try:
            request = CommonRequest(
                domain=self.http.base_url.host,
                version=self.api_version,
                action_name=action,
                uri_pattern=path,
            )
            request.set_method(method)
            request.set_accept_format("json")
            request.add_header("Content-Type", "application/json")
            request.add_header("x-acs-popcode", self.pop_code)
            if self.workspace:
                request.add_header("x-sfm-workspacecode", self.workspace)
            request.add_header("x-acs-action", action)
            if self.pop_code == "sfm" and self.settings.toolchain_app_key:
                request.add_header(
                    "x-sfm-authorization",
                    "Bearer " + self.settings.toolchain_app_key.get_secret_value(),
                )
            for key, value in (query or {}).items():
                if value is not None:
                    request.add_query_param(
                        key, str(value).lower() if isinstance(value, bool) else value
                    )
            body_values = body
            if body is not None and self.settings.toolchain_body_encoding == "form":
                # 对齐 CommonRequest.putBodyParameter 的字符串参数及 JSON Content-Type。
                # SDK 仍使用 JSON 封装，不能改成 application/x-www-form-urlencoded。
                body_values = {
                    key: value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    for key, value in body.items()
                    if value is not None
                }
                for key, value in body_values.items():
                    request.add_body_params(key, value)
            content = (
                json.dumps(body_values, ensure_ascii=False, separators=(",", ":")).encode()
                if body is not None
                else None
            )
            if content is not None:
                request.set_content(content)
            request.trans_to_acs_request()
            key_id = self.settings.toolchain_access_key_id
            secret = self.settings.toolchain_access_key_secret
            headers = request.get_signed_header(
                self.settings.toolchain_region,
                key_id.get_secret_value() if key_id else "mock-access-key",
                secret.get_secret_value() if secret else "mock-secret",
            )
            url = request.get_url(self.settings.toolchain_region, None, None)
            with self.http.stream(method, url, headers=headers, content=content) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "工具链接口请求失败，请检查授权或服务状态")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8_000_000:
                        raise HTTPException(502, "工具链响应过大，请缩小查询范围")
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or "data" not in result
            ):
                raise HTTPException(502, "工具链报告操作失败，请凭请求记录核对供应方状态")
            outcome = "succeeded"
            return result["data"]
        except httpx.TimeoutException:
            raise HTTPException(504, "工具链请求超时，结果尚未确认；请刷新状态后再操作") from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "工具链连接失败或返回格式不正确") from None
        finally:
            self._limiter.release()
            # 不记录请求正文、签名、文件地址或供应方原始错误，避免泄露内容及凭据。
            logger.info(
                "toolchain action=%s request=%s mode=%s outcome=%s elapsed_ms=%.1f",
                action,
                request_id,
                self.mode,
                outcome,
                (perf_counter() - started) * 1000,
            )
