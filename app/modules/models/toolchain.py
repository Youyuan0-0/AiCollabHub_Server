"""按 AI Studio 开发指南读取模型仓库与版本，导入前核对供应方详情。"""

from collections import defaultdict

from fastapi import HTTPException

from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import list_result, object_result
from app.modules.models.schemas import ModelInput

PAGE_SIZE = 100
FRAMEWORKS = {1: "TensorFlow", 2: "PyTorch"}


class StudioModelProvider:
    """把 AI Studio 模型成果转换为平台草稿，不推断未提供的业务字段。"""

    def __init__(self, client: PopClient) -> None:
        self.client = client

    def pages(self, path: str, action: str, query: dict | None = None) -> list[dict]:
        """按指南的 pageIndex/pageSize 读取，设置上限避免异常分页无限循环。"""
        results = []
        for page in range(1, 101):
            items = list_result(
                self.client.request(
                    "GET",
                    path,
                    action=action,
                    query={"pageIndex": page, "pageSize": PAGE_SIZE, **(query or {})},
                )
            )
            results.extend(items)
            if len(items) < PAGE_SIZE:
                return results
        raise HTTPException(502, "模型目录过大，请缩小查询范围")

    def list_models(self) -> list[dict]:
        """目录与版本分别查询；模型类型 2 仅使用目录明确给出的最新版本。"""
        models = self.pages("/model/list", "ListModel", {"modelStatus": True})
        if not models:
            return []
        types = {item.get("modelType") for item in models}
        versions: dict[str, list[dict]] = defaultdict(list)
        for model_type in (0, 1):
            if model_type not in types:
                continue
            for row in self.pages(
                "/model/list-selection",
                "ListModelSelection",
                {"modelType": model_type, "modelStatus": True},
            ):
                identifier = row.get("modelId")
                version = row.get("modelVersion")
                if isinstance(identifier, str) and isinstance(version, str) and version.strip():
                    versions[identifier].append(row)
        result = []
        seen = set()
        for row in models:
            identifier, name = row.get("modelId"), row.get("modelName")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or not isinstance(name, str)
                or not name.strip()
                or identifier in seen
            ):
                raise HTTPException(502, "AI Studio 返回的模型编号或名称无效")
            seen.add(identifier)
            choices = list(dict.fromkeys(
                item["modelVersion"] for item in versions[identifier]
            ))
            latest = row.get("latestVersion")
            if isinstance(latest, str) and latest.strip() and latest not in choices:
                choices.insert(0, latest)
            if not isinstance(latest, str) or not latest.strip():
                latest = choices[0] if choices else ""
            result.append(
                {
                    "externalId": identifier,
                    "name": name,
                    "summary": "",
                    "version": latest,
                    "versions": choices,
                    "modelType": row.get("modelType"),
                    "serviceStatus": row.get("serviceStatus"),
                }
            )
        return result

    def get_model(self, external_id: str, version: str | None = None) -> dict:
        """导入时重查目录与详情，拒绝失效版本及编号不匹配的响应。"""
        selected = next(
            (item for item in self.list_models() if item["externalId"] == external_id),
            None,
        )
        if selected is None:
            raise HTTPException(404, "AI Studio 中不存在该模型")
        chosen = version or selected["version"]
        if not chosen or chosen not in selected["versions"]:
            raise HTTPException(409, "模型版本已变化，请刷新成果后重新选择")
        detail = object_result(
            self.client.request(
                "GET", "/model/get", action="GetModel", query={"modelId": external_id}
            )
        )
        if detail.get("modelId") != external_id:
            raise HTTPException(502, "AI Studio 模型详情编号与目录不一致")
        framework = ""
        if selected["modelType"] in (0, 1):
            rows = self.pages(
                "/model/list-selection",
                "ListModelSelection",
                {"modelId": external_id, "modelType": selected["modelType"], "modelStatus": True},
            )
            matched = next((row for row in rows if row.get("modelVersion") == chosen), None)
            if matched is None or matched.get("modelId") != external_id:
                raise HTTPException(409, "模型版本已变化，请刷新成果后重新选择")
            framework = FRAMEWORKS.get(matched.get("frameWork"), "")
        source = {
            "kind": "toolchain",
            "provider": "ai-studio",
            "mode": self.client.mode,
            "environment": str(self.client.settings.toolchain_pop_url),
            "region": self.client.settings.toolchain_region,
            "externalId": external_id,
            "version": chosen,
        }
        data = ModelInput(
            name=detail.get("modelName") or selected["name"],
            version=chosen,
            framework=framework,
        ).model_dump(by_alias=True)
        return {**data, "externalId": external_id, "source": source}
