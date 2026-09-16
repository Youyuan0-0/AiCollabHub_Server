"""开发目录内的关联资源；只读取样例，不访问工具链或业务数据库。"""

from __future__ import annotations

from app.modules.common.fixtures import fixture


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
