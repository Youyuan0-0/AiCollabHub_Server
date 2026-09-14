"""供应方适配入口；当前实现只读开发样例，不调用实际服务。"""

from copy import deepcopy
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from app.modules.applications.dependencies import dependency_resources


# 供业务类查询和校验关联资源；当前使用本地样例，不执行远程模型调用。
class DemoApplicationProvider:
    def __init__(self):
        self._lock = RLock()
        self._developed = {}

    def dependencies(self):
        return dependency_resources()

    def validate_dependencies(self, dependencies):
        available = {(item["kind"], item["resourceId"]) for item in self.dependencies()}
        if any((item["kind"], item["resourceId"]) not in available for item in dependencies):
            raise HTTPException(422, "关联资源不存在，请从当前目录重新选择")

    def runtime_status(self, application_id, version_id):
        return {
            "status": "not_connected",
            "averageResponseMs": None,
            "successRate": None,
            "sampleCount": 0,
            "measuredAt": None,
        }

    def toolchain_resources(self):
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

    def develop(self, data, actor_id):
        key = "tc-dev-" + str(uuid4())
        with self._lock:
            self._developed[key] = [{"data": data, "actorId": actor_id}]
        return {"externalId": key, "externalVersion": data["version"]}

    def toolchain_resource(self, external_id, version):
        from app.modules.applications.publication_schemas import ApplicationDetails

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

    def invoke(self, resource, inputs, options, simulate_failure):
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
