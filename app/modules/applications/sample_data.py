"""仅供开发实例初始化使用的场景应用样例。"""

from __future__ import annotations

from app.modules.common.fixtures import fixture


def seed_applications() -> list[dict]:
    """生成应用目录初始样例及兼容当前接口的版本、统计与发布元数据。"""
    resources = fixture("applications", "resources")
    statistics = fixture("applications", "catalog-statistics")
    for resource in resources:
        resource.update(statistics[resource["id"]])
        resource["category"] = resource["category"].replace("法务合规", "法律合规")
        resource["tags"] = [tag.replace("法务合规", "法律合规") for tag in resource["tags"]]
        resource["facets"]["scenario"] = [resource["category"]]
        resource["facets"]["runtimeState"] = ["未接入"]
        resource.update(
            {
                "englishName": resource["id"].removeprefix("app-").replace("-", " ").title(),
                "descriptionMarkdown": (
                    f"## 应用介绍\n\n{resource['description']}\n\n"
                    "### 使用步骤\n\n1. 准备任务内容和必要材料。\n"
                    "2. 阅读使用限制并确认输入内容。\n3. 服务接入后提交任务，核对输出结果。\n\n"
                    "> 当前应用资料用于场景演示，尚未接入实际 AI 服务。"
                ),
                "baseModel": next(
                    (
                        item["value"]
                        for item in resource.get("metadata", [])
                        if item["label"] == "底层模型"
                    ),
                    "",
                ),
                "tutorials": [
                    {
                        "title": "场景应用使用说明",
                        "kind": "document",
                        "url": "/guides/scenario-applications.html",
                    }
                ],
                "configOptions": [
                    {
                        "name": "输出格式",
                        "value": "Markdown",
                        "description": "服务接入后按应用支持的格式使用。",
                    }
                ],
                "usageLimits": (
                    "请勿输入涉密信息；生成内容需要人工核对。"
                    "具体额度和文件限制以服务接入后的说明为准。"
                ),
                "serviceStatus": "not_connected",
                "publicationStatus": "published",
                "createdAt": resource["updatedAt"],
                "versionId": f"{resource['id']}-version-1",
                "technicalStack": "",
                "deployment": "",
                "triggerType": "",
                "communicationProtocol": "",
                "inputSchema": "",
                "outputSchema": "",
                "dependencies": [],
                "versionNotes": "",
            }
        )
        if resource["id"] == "app-qa":
            resource["dependencies"] = [
                {"kind": "model", "resourceId": "model-longcat"},
                {"kind": "knowledge", "resourceId": "kb-tech-docs"},
            ]
        for index, version in enumerate(resource.get("versions", [])):
            version["id"] = (
                resource["versionId"]
                if version["status"] == "current"
                else f"{resource['id']}-history-{index}"
            )
    return resources
