"""应用关联资源目录；已装配工作区时复用平台目录及其权限过滤。"""

from __future__ import annotations

from app.modules.common.fixtures import fixture
from app.modules.common.identity import Actor
from app.modules.common.resource_workspace import ResourceWorkspace
from app.modules.common.schemas import CatalogQuery
from app.modules.datasets.workspace import DatasetWorkspace
from app.modules.knowledge.service import KnowledgeService
from app.modules.tools.workspace import ToolWorkspace


def platform_dependencies(
    actor: Actor,
    datasets: DatasetWorkspace,
    knowledge: KnowledgeService,
    models: ResourceWorkspace,
    tools: ToolWorkspace,
) -> list[dict]:
    """仅列出已发布且当前账号可见的资源，避免把固定样例当作真实依赖。"""
    sources = {
        "dataset": datasets.resources(actor),
        # 本地知识库以创建审批和开放范围控制依赖访问，不使用工具链的整库上下架状态。
        "knowledge": knowledge.published_resources(actor),
        "model": [item for item in models.catalog(actor) if item["listingStatus"] == "listed"],
        "tool": [],
    }
    page = 1
    while True:
        result = tools.catalog(CatalogQuery(page=page, pageSize=100), actor)
        sources["tool"].extend(result["items"])
        if len(sources["tool"]) >= result["total"] or not result["items"]:
            break
        page += 1
    return [
        {
            "kind": kind,
            "resourceId": resource["id"],
            "name": resource["name"],
            "owner": resource.get("owner", ""),
            "version": resource.get("version", ""),
        }
        for kind, resources in sources.items()
        for resource in resources
    ]


def dependency_resources() -> list[dict]:
    """合并模型、数据集、知识库和工具样例为应用关联资源目录。"""
    sources = {
        "model": ("models", "resources"),
        "dataset": ("datasets", "resources"),
        "knowledge": ("knowledge", "bases"),
        "tool": ("tools", "resources"),
    }
    return [
        {
            "kind": kind,
            "resourceId": resource["id"],
            "name": resource["name"],
            "owner": resource["owner"],
            "version": resource.get("version", ""),
        }
        for kind, (module, name) in sources.items()
        for resource in fixture(module, name)
    ]
