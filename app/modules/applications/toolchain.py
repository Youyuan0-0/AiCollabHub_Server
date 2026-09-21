"""Agent Dev 目录适配，按 P15–27 查询智能体和版本，复用平台发布审核。"""

from fastapi import HTTPException

from app.modules.applications.publication_schemas import ApplicationDetails
from app.modules.common.identity import Actor
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import (
    authorize,
    list_result,
    object_result,
    source_reference,
)

BASE = "/gatectl/agent/openapi/agent"


class AgentCatalogProvider:
    """只替换成果目录与来源，应用运行继续由独立运行适配器负责。"""

    provider_name = "agent-dev"

    def __init__(self, delegate: object, client: PopClient) -> None:
        self.delegate = delegate
        self.client = client
        self.toolchain_mode = client.mode

    def __getattr__(self, name: str) -> object:
        """委托目录之外的现有能力，避免重新实现审核和运行。"""
        return getattr(self.delegate, name)

    def authorize(self, actor: Actor) -> None:
        """校验服务账号对应工作空间的访问授权。"""
        authorize(self.client.settings, actor)

    def reference(self, identifier: str) -> dict:
        """返回带环境和工作空间的外部来源。"""
        return source_reference(self.client.settings, self.provider_name, identifier)

    def pages(self, path: str, action: str, payload: dict) -> list[dict]:
        """读取有上限的完整目录，拒绝分页异常导致静默缺失版本。"""
        items = []
        for current in range(1, 101):
            data = object_result(
                self.client.request(
                    "POST",
                    BASE + path,
                    action=action,
                    body={**payload, "current": current, "pageSize": 100},
                )
            )
            page = list_result(data.get("list"))
            items.extend(page)
            total = data.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise HTTPException(502, "工具链返回的分页总数无效")
            if len(items) >= total:
                return items
            if not page:
                raise HTTPException(502, "工具链分页提前结束，请刷新重试")
        raise HTTPException(502, "工具链目录过大，请缩小工作空间范围")

    def versions(self, identifier: str) -> list[dict]:
        """运行使用 agentVersion 编码，versionName 仅用于展示。"""
        return self.pages(
            "/version/pageQuery",
            "PostAgentOpenapiAgentVersionPagequery",
            {"agentCode": identifier, "versionName": ""},
        )

    def toolchain_resources(self) -> list[dict]:
        """转换文档里的列表字段，为原有选择版本页面提供统一契约。"""
        agents = self.pages("/pageQuery", "PostAgentOpenapiAgentPagequery", {"agentName": ""})
        resources = []
        for agent in agents:
            identifier = agent.get("agentCode")
            if not isinstance(identifier, str):
                raise HTTPException(502, "工具链返回的智能体编号无效")
            if agent.get("workspaceCode") not in (None, self.client.workspace):
                continue
            versions = self.versions(identifier)
            resources.append(
                {
                    "externalId": identifier,
                    "workspace": self.client.workspace,
                    "name": agent.get("agentName", ""),
                    "summary": agent.get("agentDesc", ""),
                    "category": "",
                    "updatedAt": agent.get("gmtModified"),
                    "versions": [
                        str(item["agentVersion"])
                        for item in versions
                        if item.get("agentCode") == identifier and item.get("agentVersion")
                    ],
                    "versionDetails": [
                        {
                            "code": str(item.get("agentVersion", "")),
                            "name": item.get("versionName", ""),
                            "status": item.get("agentStatus", ""),
                        }
                        for item in versions
                    ],
                }
            )
        return resources

    def toolchain_resource(self, external_id: str, version: str) -> dict:
        """按文档获取指定版本；供应方未提供的必填项留待平台草稿补充。"""
        resource = next(
            (item for item in self.toolchain_resources() if item["externalId"] == external_id), None
        )
        if resource is None or version not in resource["versions"]:
            raise HTTPException(404, "当前工作空间不存在该智能体或版本")
        selected = next(
            (
                item
                for item in self.versions(external_id)
                if str(item.get("agentVersion")) == version and item.get("agentCode") == external_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(409, "智能体版本已变化，请刷新")
        data = ApplicationDetails(
            name=resource["name"],
            summary=resource["summary"][:1000],
            version=version,
            description_markdown=selected.get("agentDefinition") or "",
        ).model_dump(by_alias=True)
        return {
            "externalId": external_id,
            "workspace": self.client.workspace,
            "data": data,
            "source": self.reference(external_id),
            "agentStatus": selected.get("agentStatus"),
        }

    def develop(self, data: dict, actor_id: str) -> dict:
        """专业编排在供应方工作台完成，不能将本地表单误报为外部开发成功。"""
        raise HTTPException(409, "请进入配置的工具链工作台完成开发，再返回导入成果")
