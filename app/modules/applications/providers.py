"""供应方适配入口；当前实现只读开发样例，不调用实际服务。"""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from app.modules.applications.demo_marketplace import DemoMarketplaceStore
from app.modules.applications.dependencies import dependency_resources


# 供业务类查询和校验关联资源；当前使用本地样例，不执行远程模型调用。
class DemoApplicationProvider:
    """使用本地样例提供关联资源、工具链资料与模拟执行结果。"""

    def __init__(self, marketplace: DemoMarketplaceStore | None = None) -> None:
        """可选绑定文件样例存储；未提供时开发资料仅保存在进程内。"""
        self._lock = RLock()
        self._developed = {}
        self.marketplace = marketplace

    def dependencies(self) -> list[dict]:
        """读取当前演示关联资源目录。"""
        return dependency_resources()

    def validate_dependencies(self, dependencies: list[dict]) -> None:
        """按资源类型与编号检查关联资源存在性。"""
        available = {(item["kind"], item["resourceId"]) for item in self.dependencies()}
        if any((item["kind"], item["resourceId"]) not in available for item in dependencies):
            raise HTTPException(422, "关联资源不存在，请从当前目录重新选择")

    def runtime_status(self, resource: dict) -> dict:
        """返回未接入状态和空统计，不虚构实际运行指标。"""
        return {
            "status": "not_connected",
            "averageResponseMs": None,
            "successRate": None,
            "sampleCount": 0,
            "measuredAt": None,
        }

    def toolchain_resources(self) -> list[dict]:
        """合并内置样例与已提交开发资料，提供可选版本列表。"""
        self._load_developments()
        base = [
            {
                "externalId": external_id,
                "workspace": "演示工具链工作空间",
                "name": name,
                "versions": ["v1.0", "v2.0"],
                "updatedAt": "2026-09-14T00:00:00+00:00",
                "summary": summary,
                "category": category,
            }
            for external_id, name, summary, category in (
                ("tc-report", "工具链报告助手", "根据输入整理报告框架", "文档创作"),
                ("tc-meeting", "工具链会议助手", "归纳会议要点和待办", "会议办公"),
                ("tc-translate", "工具链翻译助手", "处理技术资料翻译任务", "翻译沟通"),
            )
        ]

        with self._lock:
            custom = [
                {
                    "externalId": key,
                    "workspace": "模拟开发工作空间",
                    "name": versions[-1]["data"]["name"],
                    "summary": versions[-1]["data"]["summary"],
                    "category": versions[-1]["data"]["category"],
                    "versions": [item["data"]["version"] for item in versions],
                }
                for key, versions in self._developed.items()
            ]
        return base + custom

    def develop(self, data: dict, actor_id: str) -> dict:
        """保存演示开发资料，返回工具链资源和版本编号。"""
        if self.marketplace is not None:
            reference = self.marketplace.save("development", {"data": data, "actorId": actor_id})
            key = "tc-dev-" + reference.removeprefix("development-")
            return {"externalId": key, "externalVersion": data["version"]}
        key = "tc-dev-" + str(uuid4())
        with self._lock:
            self._developed[key] = [{"data": data, "actorId": actor_id}]
        return {"externalId": key, "externalVersion": data["version"]}

    def _load_developments(self) -> None:
        """从模拟存储恢复开发资料，并按快照引用建立可查询记录。"""
        if self.marketplace is not None:
            with self._lock:
                self._developed = {
                    "tc-dev-" + key.removeprefix("development-"): [record]
                    for key, record in self.marketplace.developments()
                }

    def toolchain_resource(self, external_id: str, version: str) -> dict:
        """读取指定工具链版本的发布资料，不存在时返回 404。"""
        from app.modules.applications.publication_schemas import ApplicationDetails

        self._load_developments()
        with self._lock:
            custom = next(
                (
                    item
                    for item in self._developed.get(external_id, [])
                    if item["data"]["version"] == version
                ),
                None,
            )
        if custom:
            return {
                "externalId": external_id,
                "workspace": "模拟开发工作空间",
                "data": deepcopy(custom["data"]),
            }
        item = next(
            (item for item in self.toolchain_resources() if item["externalId"] == external_id), None
        )
        if item is None or version not in item["versions"]:
            raise HTTPException(404, "工具链应用或版本不存在")
        return {
            **item,
            "data": ApplicationDetails(
                name=item["name"],
                version=version,
                summary=item["summary"],
                category=item["category"],
                owner="演示工具链团队",
                description_markdown=f"## {item['name']}\n\n{item['summary']}",
                tags=[item["category"], "工具链"],
                capabilities=["结构化输出"],
                technical_stack="工具链托管",
                deployment="工具链工作空间",
                trigger_type="用户触发",
                communication_protocol="HTTP",
                input_schema='{"type":"object","properties":{"question":{"type":"string","minLength":1}},"required":["question"]}',
                usage_limits="当前为模拟服务，输出用于操作流程验证。",
                version_notes=f"工具链 {version} 版本",
            ).model_dump(by_alias=True),
        }

    def invoke(self, resource: dict, inputs: dict, options: dict, simulate_failure: bool) -> dict:
        """返回包含输入和配置的模拟结果，可按参数触发模拟失败。"""
        if simulate_failure:
            raise RuntimeError("simulated provider failure")
        return {
            "text": (
                f"【模拟结果】{resource['name']}已接收任务。\n实际AI服务接入后，此处显示真实输出。"
            ),
            "receivedInput": inputs,
            "appliedOptions": options,
            "mode": "simulation",
        }
