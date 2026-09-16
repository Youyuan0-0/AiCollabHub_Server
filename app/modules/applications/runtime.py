"""Agent Dev 普通智能体运行适配，依据开发指南 §1.4.1.1（P216–223）。"""

import json
from pathlib import Path
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class AgentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str = Field(min_length=1)
    agent_code: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    input_mode: Literal["text", "json"] = "text"


class AgentDevProvider:
    """运行走正式协议；目录/发布仍由独立适配器负责，不以运行成功推断已发布。"""

    mode = "live"

    def __init__(self, catalog_provider, settings, transport=None):
        self.catalog_provider = catalog_provider
        self.settings = settings
        self.transport = transport

    def __getattr__(self, name):
        return getattr(self.catalog_provider, name)

    def binding(self, resource):
        settings = self.settings
        if not (
            settings.applications_agent_url
            and settings.applications_agent_key
            and settings.applications_agent_bindings
        ):
            raise HTTPException(503, "实际运行服务尚未配置地址、凭证和应用对应关系")
        try:
            bindings = TypeAdapter(dict[str, AgentBinding]).validate_json(
                Path(settings.applications_agent_bindings).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError):
            raise HTTPException(503, "应用运行对应关系读取失败，请联系管理员") from None
        binding = bindings.get(resource["id"])
        if not binding or binding.version_id != resource["versionId"]:
            raise HTTPException(503, "当前应用版本尚未配置实际智能体")
        return binding

    def connection(self, resource):
        try:
            self.binding(resource)
        except HTTPException as error:
            return {"mode": "live", "configured": False, "message": error.detail}
        return {"mode": "live", "configured": True, "message": "已配置实际运行服务"}

    def runtime_status(self, application_id, version_id):
        configured = self.connection({"id": application_id, "versionId": version_id})["configured"]
        return {
            "status": "configured" if configured else "not_connected",
            "averageResponseMs": None,
            "successRate": None,
            "sampleCount": 0,
            "measuredAt": None,
        }

    def invoke(self, resource, inputs, options, simulate_failure):
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
                    "stream": False,
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
            "text": "\n".join(texts),
            "images": images,
            "sessionId": session_id,
            "requestId": data.get("requestId"),
        }

    @staticmethod
    def post(client, path, payload):
        try:
            with client.stream("POST", path, json=payload) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "智能体接口请求失败，请检查服务授权和运行状态")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise HTTPException(502, "智能体返回内容过大")
                    chunks.append(chunk)
                result = json.loads(b"".join(chunks))
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
