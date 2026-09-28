"""工具运行契约和本地模拟供应方；真实成果查询由 toolchain 适配器装配。"""

import json
from copy import deepcopy
from typing import Protocol

from fastapi import HTTPException

from app.modules.common.fixtures import fixture
from app.modules.tools.fields import field_rules

INPUT_SCHEMA = (
    '{"type":"object","properties":{"text":{"type":"string","minLength":1}},'
    '"required":["text"],"additionalProperties":false}'
)
OUTPUT_SCHEMA = '{"type":"object","properties":{"result":{"type":"string"}},"required":["result"]}'


def default_configuration() -> dict:
    """复用现有原型分类，附加任务字段规则与演示预检词。"""
    return {
        "revision": 1,
        "toolTypes": [
            "文本处理",
            "图像处理",
            "语音处理",
            "数据分析",
            "代码开发",
            "模型工具",
            "智能体工具",
        ],
        "sourceTypes": ["自研工具", "第三方接入", "开源集成"],
        "deployments": ["平台托管", "API接入", "独立部署"],
        "fields": field_rules(),
        "sensitiveTerms": ["演示拦截词"],
    }


def seed_tools() -> list[dict]:
    """复用已有十二个目录样例，补齐发布字段并重置为本次演示的真实活动计数。"""
    resources = fixture("tools", "resources")
    for item in resources:
        item.update(
            {
                "descriptionMarkdown": item["description"],
                "toolType": item["category"],
                "sourceType": item["facets"]["sourceType"][0],
                "team": item["owner"],
                "technicalStack": "Python / REST",
                "environment": "由工具提供方维护",
                "deployment": item["facets"]["deployment"][0],
                "inputSchema": INPUT_SCHEMA,
                "outputSchema": OUTPUT_SCHEMA,
                "inputExample": '{"text":"请处理这段示例内容"}',
                "outputExample": '{"result":"模拟响应：示例处理结果"}',
                "dataLevel": "一般数据",
                "endpoint": "",
                "usageExample": "以输入 JSON 运行模拟体验，输出为演示内容。",
                "createdBy": "demo-publisher",
                "createdAt": item["updatedAt"],
                "source": {
                    "kind": "sample",
                    "label": "初始模拟资料",
                    "externalId": "",
                    "syncedAt": None,
                },
                "organizationId": "org-research",
                "contact": "研发发布者",
                "listingStatus": "listed",
                "revision": 1,
                "runtimeMode": "simulation",
                "usage": 0,
                "favoriteCount": 0,
                "rating": None,
                "ratingCount": 0,
                "status": "available",
                "metrics": [],
                "versions": [
                    {
                        "version": item["version"],
                        "releasedAt": item["updatedAt"],
                        "status": "current",
                        "note": "初始模拟资料",
                    }
                ],
            }
        )
        item["facets"].update({"tags": item["tags"], "team": [item["team"]]})
    return resources


class ToolProvider(Protocol):
    """平台与工具链之间的契约；鉴权和供应方字段转换留在实现内部。"""

    mode: str

    def entry_url(self) -> str:
        """返回工具链开发入口，真实适配器提供其配置的工作台地址。"""
        ...

    def list_tools(self) -> list[dict]:
        """读取可导入工具的元数据，不写入平台目录。"""
        ...

    def get_tool(self, external_id: str, version: str | None = None) -> dict:
        """读取完整工具元数据，对应工具链 getOne 接口。"""
        ...

    def invoke(self, resource: dict, inputs: object, simulate_failure: bool) -> object:
        """执行单个工具，返回统一结果；业务失败抛出 HTTPException。"""
        ...


class MockToolProvider:
    """仅提供工具链样例与模拟运行，不访问资料中填写的地址。"""

    mode = "simulation"

    def entry_url(self) -> str:
        """本地工作台仅模拟选择开发结果，不实现外部工具编排器。"""
        return "/tools/toolchain/workbench"

    def list_tools(self) -> list[dict]:
        """复用现有样例作为工具链导入候选，并使用稳定外部编号。"""
        return [
            {**deepcopy(item), "externalId": "agent-dev-" + item["id"]} for item in seed_tools()[:3]
        ]

    def invoke(self, resource: dict, inputs: object, simulate_failure: bool) -> object:
        """显式标记演示结果，失败路径用于校验统计与错误反馈。"""
        if simulate_failure:
            raise HTTPException(502, "模拟工具调用失败，请重试")
        if resource.get("outputExample"):
            return json.loads(resource["outputExample"])
        return {"result": f"{resource['name']} 的模拟响应：{str(inputs)[:500]}"}

    def get_tool(self, external_id: str, version: str | None = None) -> dict:
        """根据稳定外部编号返回模拟详情。"""
        tool = next((item for item in self.list_tools() if item["externalId"] == external_id), None)
        if tool is None:
            raise HTTPException(404, "工具链资源不存在")
        if version is not None and tool["version"] != version:
            raise HTTPException(409, "工具链版本已变化，请刷新成果后重新核对")
        return tool


def create_tool_provider() -> ToolProvider:
    """真实工具链接入的替换点：在此返回实现相同协议的 Agent Dev/API/MCP 适配器。"""
    return MockToolProvider()
