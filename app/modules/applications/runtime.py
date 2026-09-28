"""Agent Dev 普通智能体运行适配，依据开发指南 §1.4.1.1（P216–223）。"""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.core.config import Settings
from app.modules.applications.ports import ApplicationProvider
from app.modules.common.toolchain.network import http_options


class AgentBinding(BaseModel):
    """平台应用版本到实际智能体编号、版本及输入格式的可信映射。"""

    model_config = ConfigDict(extra="forbid")
    version_id: str = Field(min_length=1)
    agent_code: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    input_mode: Literal["text", "json"] = "text"


class AgentDevProvider:
    """运行走正式协议；目录/发布仍由独立适配器负责，不以运行成功推断已发布。"""

    mode = "live"

    def __init__(
        self,
        catalog_provider: ApplicationProvider,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """绑定目录适配器、实际服务配置和可替换的 HTTP 传输。"""
        self.catalog_provider = catalog_provider
        self.settings = settings
        self.transport = transport

    def __getattr__(self, name: str) -> object:
        """将目录和发布能力委托给已有提供方。"""
        return getattr(self.catalog_provider, name)

    def binding(self, resource: dict) -> AgentBinding:
        """从服务端配置或可信导入来源解析当前版本的智能体绑定。

        显式绑定必须同时匹配平台 resourceId 和 versionId，资源发布新版后需同步
        更新映射。使用导入来源时还要核对工作空间、环境和上线状态，不能把客户端
        提交的外部编号或其他环境的资源当作已授权的运行目标。
        """
        settings = self.settings
        if not (settings.applications_agent_url and settings.applications_agent_key):
            raise HTTPException(503, "实际运行服务尚未配置地址、凭证和应用对应关系")
        try:
            adapter = TypeAdapter(dict[str, AgentBinding])
            if settings.applications_agent_bindings_data is not None:
                bindings = adapter.validate_python(settings.applications_agent_bindings_data)
            elif settings.applications_agent_bindings:
                bindings = adapter.validate_json(
                    Path(settings.applications_agent_bindings).read_text(encoding="utf-8")
                )
            else:
                bindings = {}
        except (OSError, ValidationError):
            raise HTTPException(503, "应用运行对应关系读取失败，请联系管理员") from None
        binding = bindings.get(resource["id"])
        if binding:
            if binding.version_id == resource["versionId"]:
                return binding
            raise HTTPException(503, "当前应用版本尚未配置实际智能体")
        # 手工绑定只覆盖对应应用，其他通过可信工具链导入的应用仍可使用来源信息。
        source = resource.get("source", {})
        if (
            source.get("provider") == "agent-dev"
            and source.get("mode") == "live"
            and source.get("workspace") == settings.toolchain_workspace
            and source.get("environment") == str(settings.toolchain_pop_url)
            and source.get("externalStatus") == "ONLINE"
            and isinstance(source.get("externalId"), str)
            and source.get("externalId")
            and isinstance(source.get("externalVersion"), str)
            and source.get("externalVersion")
        ):
            return AgentBinding(
                version_id=resource["versionId"],
                agent_code=source["externalId"],
                agent_version=source["externalVersion"],
            )
        raise HTTPException(503, "当前应用版本尚未绑定可运行的智能体")

    def connection(self, resource: dict) -> dict:
        """将实际运行配置的可用性转换为前端连接状态。"""
        try:
            self.binding(resource)
        except HTTPException as error:
            return {"mode": "live", "configured": False, "message": error.detail}
        return {"mode": "live", "configured": True, "message": "已配置实际运行服务"}

    def runtime_status(self, resource: dict) -> dict:
        """返回配置接入状态，运行统计交由平台实际任务记录提供。"""
        configured = self.connection(resource)["configured"]
        return {
            "status": "configured" if configured else "not_connected",
            "averageResponseMs": None,
            "successRate": None,
            "sampleCount": 0,
            "measuredAt": None,
        }

    def invoke(self, resource: dict, inputs: dict, options: dict, simulate_failure: bool) -> dict:
        """创建会话并按配置接收 JSON 或 SSE，统一返回经过校验的最终结果。"""
        if simulate_failure:
            raise HTTPException(422, "实际服务不支持模拟失败选项")
        binding = self.binding(resource)
        if binding.input_mode == "text":
            if set(inputs) != {"question"} or not isinstance(inputs["question"], str):
                raise HTTPException(422, "当前智能体接收 question 文本，请按输入说明填写")
            message = inputs["question"]
        else:
            message = json.dumps(inputs, ensure_ascii=False)
        # 不跟随跳转、不自动重试创建/运行，防止凭证转发或重复执行真实任务。
        with httpx.Client(
            base_url=str(self.settings.applications_agent_url).rstrip("/"),
            headers={
                "Authorization": "Bearer " + self.settings.applications_agent_key.get_secret_value()
            },
            timeout=self.settings.applications_agent_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
            **http_options(
                self.settings.applications_agent_proxy_url if self.transport is None else None,
                self.settings.applications_agent_ca_certificate,
            ),
        ) as client:
            session = self.post(
                client,
                "/gateway/agent/api/createSession",
                {
                    "agentCode": binding.agent_code,
                    "agentVersion": binding.agent_version,
                },
            )
            session_id = session.get("uniqueCode")
            if not isinstance(session_id, str) or not session_id:
                raise HTTPException(502, "智能体服务未返回有效会话编号")
            data = self.post(
                client,
                "/gateway/agent/api/run",
                {
                    "sessionId": session_id,
                    "stream": self.settings.applications_agent_response_mode == "sse",
                    "delta": True,
                    "trace": False,
                    "message": {
                        "text": message,
                        "metadata": {"options": options},
                        "attachments": [],
                    },
                },
            )
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or not content:
            raise HTTPException(502, "智能体未返回最终内容，无法确认运行成功")
        texts, images = [], []
        for item in content:
            if not isinstance(item, dict):
                raise HTTPException(502, "智能体返回内容格式不正确")
            value = item.get("text") if item.get("type") == "text" else None
            if isinstance(value, dict) and isinstance(value.get("value"), str):
                texts.append(value["value"])
            elif item.get("type") == "image" and isinstance(item.get("image"), dict):
                url = item["image"].get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    images.append(url)
        if not texts and not images:
            raise HTTPException(502, "智能体返回内容类型尚不支持")
        return {
            "mode": "live",
            "text": ("" if self.settings.applications_agent_response_mode == "sse" else "\n").join(
                texts
            ),
            "images": images,
            "sessionId": session_id,
            "requestId": data.get("requestId"),
        }

    @staticmethod
    def stream_result(raw: bytes, session_id: str) -> dict:
        """将一次完整接收的 SSE 字节串合并为最终内容，不向前端逐段转发。

        只采纳 message.delta 的 content，校验 sessionId 和结束标记；供应方可能
        返回其他事件，不能将其作为答案拼接。必须收到 end=True 才确认完成，
        结束后的增量、会话不匹配或连接提前断开都不能作为成功结果保存。
        """
        content, request_id, ended = [], None, False
        for block in raw.decode("utf-8-sig").replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip(" ") for line in block.splitlines() if line.startswith("data:")
            )
            if not data:
                continue
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError("invalid event")
            if event.get("object") == "error":
                raise HTTPException(502, "智能体服务报告运行失败")
            if event.get("object") != "message.delta":
                continue
            if (
                ended
                or event.get("sessionId") != session_id
                or not isinstance(event.get("end"), bool)
            ):
                raise ValueError("invalid session or event order")
            items = event.get("content")
            if not isinstance(items, list):
                raise ValueError("invalid content")
            content.extend(items)
            request_id = event.get("requestId") or request_id
            ended = event["end"]
        if not ended:
            raise HTTPException(502, "智能体流式响应中断，未收到完成事件")
        return {"message": {"content": content}, "requestId": request_id}

    @staticmethod
    def post(client: httpx.Client, path: str, payload: dict) -> dict:
        """发送智能体请求并限制响应大小，不自动重试可能已执行的操作。"""
        started = monotonic()
        try:
            with client.stream("POST", path, json=payload) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "智能体接口请求失败，请检查服务授权和运行状态")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if monotonic() - started > (client.timeout.read or 120):
                        raise httpx.ReadTimeout("request deadline exceeded")
                    size += len(chunk)
                    if size > 2_000_000:
                        raise HTTPException(502, "智能体返回内容过大")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                if payload.get("stream") is True:
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ValueError("invalid stream content type")
                    return AgentDevProvider.stream_result(raw, payload["sessionId"])
                result = json.loads(raw)
        except httpx.TimeoutException:
            raise HTTPException(
                504, "等待智能体结果超时，完成状态未确认；请先核实任务状态"
            ) from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "智能体服务连接失败或返回格式不正确") from None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise HTTPException(502, "智能体服务报告运行失败")
        if not isinstance(result.get("data"), dict):
            raise HTTPException(502, "智能体返回数据格式不正确")
        return result["data"]
