"""按 Agent Dev 开发指南 P35–46 读取工具成果，复用现有 POP 签名与受控传输。"""

from fastapi import HTTPException
from pydantic import ValidationError

from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import list_result, object_result, source_reference
from app.modules.tools.providers import ToolProvider
from app.modules.tools.schemas import ToolInput

BASE = "/gatectl/agent/openapi/tool"


class AgentToolCatalogProvider:
    """只替换工具成果查询；实际工具执行继续使用独立的运行供应方。"""

    def __init__(self, delegate: ToolProvider, client: PopClient) -> None:
        self.delegate = delegate
        self.client = client

    def __getattr__(self, name: str) -> object:
        """委托已有运行能力和运行模式，不将成果连接误报为真实执行。"""
        return getattr(self.delegate, name)

    def entry_url(self) -> str:
        """只使用管理员保存的工作台地址，真实模式不跳转本地模拟工作台。"""
        return str(self.client.settings.toolchain_tool_entry_url or "")

    def normalize(self, value: dict) -> dict:
        """只导入文档定义的公开元数据，不向浏览器传递含鉴权信息的 serviceConfig。"""
        code, version = value.get("code"), value.get("version")
        if not isinstance(code, str) or not code.strip() or len(code) > 200:
            raise HTTPException(502, "工具链返回的工具编号无效")
        if not isinstance(version, str) or not version.strip():
            raise HTTPException(502, "工具链返回的工具版本无效")
        if not isinstance(value.get("name"), str) or not value["name"].strip():
            raise HTTPException(502, "工具链返回的工具名称无效")
        try:
            data = ToolInput(
                name=value["name"],
                version=version,
                description=value.get("desc") or "",
                description_markdown=value.get("desc") or "",
            ).model_dump(by_alias=True)
        except ValidationError:
            raise HTTPException(502, "工具链资料格式不符合要求，请核对供应方返回内容") from None
        return {
            **data,
            "externalId": code,
            "externalToolType": value.get("toolType")
            if isinstance(value.get("toolType"), str)
            else "",
            "source": source_reference(self.client.settings, "agent-dev", code),
        }

    def list_tools(self) -> list[dict]:
        """按文档要求的范围和类型分页查询；分页异常明确报错，不回退为样例。"""
        items: dict[str, dict] = {}
        settings = self.client.settings
        for tool_type in dict.fromkeys(settings.toolchain_tools_types):
            count = 0
            seen = set()
            for current in range(1, 101):
                result = object_result(
                    self.client.request(
                        "POST",
                        BASE + "/pageQuery",
                        action="PostAgentOpenapiToolPagequery",
                        body={
                            "current": current,
                            "pageSize": 100,
                            "toolCategory": settings.toolchain_tools_category,
                            "toolType": tool_type,
                        },
                    )
                )
                page, total = list_result(result.get("list")), result.get("total")
                if type(total) is not int or total < 0:
                    raise HTTPException(502, "工具链返回的分页总数无效")
                count += len(page)
                for row in page:
                    if row.get("workspaceCode") != self.client.workspace:
                        continue
                    item = self.normalize(row)
                    identifier = item["externalId"]
                    if identifier in seen or (
                        identifier in items and items[identifier]["version"] != item["version"]
                    ):
                        raise HTTPException(502, "工具链分页包含重复或冲突的成果，请刷新重试")
                    seen.add(identifier)
                    items[identifier] = item
                if count >= total:
                    break
                if not page:
                    raise HTTPException(502, "工具链分页提前结束，请刷新重试")
            else:
                raise HTTPException(502, "工具链目录过大，请缩小查询范围")
        return list(items.values())

    def get_tool(self, external_id: str, version: str | None = None) -> dict:
        """核对当前工作空间和所选版本后读取详情，不能越权导入或静默换成新版本。"""
        selected = next(
            (item for item in self.list_tools() if item["externalId"] == external_id), None
        )
        if selected is None:
            raise HTTPException(404, "当前工具链工作空间不存在该成果")
        if version is not None and version != selected["version"]:
            raise HTTPException(409, "工具链版本已变化，请刷新成果后重新核对")
        result = object_result(
            self.client.request(
                "POST",
                BASE + "/getOne",
                action="PostAgentOpenapiToolGetone",
                body={"uniqueCode": external_id, "version": selected["version"]},
            )
        )
        if result.get("workspaceCode") != self.client.workspace:
            raise HTTPException(404, "工具链详情不属于当前工作空间")
        if result.get("code") != external_id or result.get("version") != selected["version"]:
            raise HTTPException(409, "工具链详情已变化，请刷新成果后重新核对")
        return self.normalize(result)
