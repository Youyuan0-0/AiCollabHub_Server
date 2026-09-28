"""模型专属字段映射和工具链边界，共用发布审核状态机。"""

from typing import Protocol

from fastapi import Request

from app.modules.common.fixtures import fixture
from app.modules.common.resource_workspace import ResourceWorkspace
from app.modules.models.schemas import ModelInput


class ModelProvider(Protocol):
    """真实训推平台适配入口，仅同步元数据，平台不实现训练推理。"""

    def list_models(self) -> list[dict]:
        """查询可以拉取的外部模型元数据。"""
        ...

    def get_model(self, external_id: str, version: str | None = None) -> dict:
        """重新读取选中版本的元数据。"""
        ...


class SimulatedModelProvider:
    """读取模型样例并补齐明确标识为模拟的专属字段。"""

    def list_models(self) -> list[dict]:
        """映射原样例字段，展示统计不作为真实活动。"""
        result = []
        for seed in fixture("models", "resources"):
            metadata = {item["label"]: item["value"] for item in seed.get("metadata", [])}
            values = ModelInput(
                name=seed["name"],
                version=seed.get("version", "v1.0"),
                summary=seed["description"],
                tags=seed["tags"],
                task_type=seed["category"],
                architecture=metadata.get("模型架构", ""),
                parameters=metadata.get("参数规模", ""),
                context_length=metadata.get("上下文长度", ""),
                framework=seed.get("framework", "Transformers"),
                model_format="Safetensors",
                precision="BF16",
                config_json='{"model_type":"simulation"}',
                license="模拟内部许可",
                service_capabilities=[
                    item.removeprefix("支持") for item in seed.get("facets", {}).get("support", [])
                ],
                model_status=seed["badge"]
                if seed.get("badge") in ("NEW", "合集", "推进", "推荐", "稳定", "弃用")
                else "NEW",
                capabilities=seed.get("capabilities", []),
            ).model_dump(by_alias=True)
            result.append(
                {
                    **seed,
                    **values,
                    "downloadCount": 0,
                    "favoriteCount": 0,
                    "rating": None,
                    "usage": 0,
                    "externalId": seed["id"],
                    "versions": [values["version"]],
                    "sourceType": "第三方引入",
                    "trainingDatasetIds": [],
                    "securityLevel": "内部",
                    "mode": "simulation",
                }
            )
        return result

    def get_model(self, external_id: str, version: str | None = None) -> dict:
        """模拟工作台也核对选中编号和版本。"""
        from fastapi import HTTPException

        selected = next(
            (item for item in self.list_models() if item["externalId"] == external_id),
            None,
        )
        if selected is None:
            raise HTTPException(404, "工具链模型不存在")
        if version and version != selected["version"]:
            raise HTTPException(409, "模型版本已变化，请刷新成果后重新选择")
        return {
            **selected,
            "source": {
                "kind": "toolchain",
                "provider": "ai-studio",
                "mode": "mock",
                "environment": "mock",
                "region": "",
                "externalId": external_id,
                "version": selected["version"],
            },
        }


def create_model_provider() -> ModelProvider:
    """集中装配模拟供应方，后续在此替换真实元数据接口。"""
    return SimulatedModelProvider()


def model_workspace(request: Request) -> ResourceWorkspace:
    """取得当前服务实例的模型仓储。"""
    return request.app.state.model_workspace


def public_model(item: dict) -> dict:
    """普通详情移除管理员维护字段，保留卡片与详情要求。"""
    return {
        key: value
        for key, value in item.items()
        if key
        not in (
            "visibility",
            "organizationId",
            "createdBy",
            "contact",
            "securityLevel",
            "sourceType",
            "trainingDatasetIds",
            "versions",
            "source",
            "ownerAvailable",
        )
    }
