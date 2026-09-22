"""将原型样例转换为当前发布资料，不包含虚构活动、旧版本或文件记录。"""

import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from app.modules.applications.publication_schemas import ApplicationDetails
from app.modules.applications.sample_data import seed_applications
from app.modules.common.fixtures import fixture
from app.modules.datasets.schemas import DatasetInput, validate_submission
from app.modules.models.schemas import ModelInput
from app.modules.models.workspace import SimulatedModelProvider
from app.modules.tools.providers import seed_tools
from app.modules.tools.schemas import ToolInput

IMPORT_NOTE = "原型 Mock 资料导入，仅供开发测试；外部服务仍为模拟。"


@dataclass(frozen=True)
class DemoResource:
    """保留样例来源编号，平台 UUID 与后续资料修改无关。"""

    kind: str
    sample_id: str
    data: dict
    is_public: bool = True

    def identifier(self, entity: str = "resource") -> str:
        """以固定命名空间生成资源、申请、版本和审核的独立 UUID。"""
        return str(
            uuid5(NAMESPACE_URL, f"aicollabhub:demo:v1:{self.kind}:{self.sample_id}:{entity}")
        )


def form_values(model: type[BaseModel], values: dict) -> dict:
    """只提取发布表单字段，排除旧目录中的派生指标和内部编号。"""
    names = {field.alias or name for name, field in model.model_fields.items()}
    return model.model_validate(
        {key: value for key, value in values.items() if key in names}
    ).model_dump(by_alias=True)


def dataset_values(sample: dict) -> dict:
    """保留数据集概览、字段和三行预览，大小表示样例声明的规模。"""
    metadata = {item["label"]: item["value"] for item in sample["metadata"]}
    fields = sample["schema"]
    schema = {
        "type": "object",
        "properties": {
            field["name"]: {
                "type": "string" if field["type"] in {"text", "uri"} else field["type"],
                "description": field["description"],
                "x-source-type": field["type"],
            }
            for field in fields
        },
        "required": [field["name"] for field in fields if field.get("required")],
    }
    preview = sample["preview"]
    rows = [dict(zip(preview["columns"], row, strict=True)) for row in preview["rows"]]
    data = DatasetInput(
        name=sample["name"],
        english_name=sample["name"],
        version=sample["version"],
        summary=sample["description"],
        description_markdown=sample["description"] + "\n\n> " + IMPORT_NOTE,
        owner=sample["owner"],
        contact="模拟资料维护人",
        tags=sample["tags"],
        data_application=sample["facets"]["dataApplication"],
        data_shape=sample["facets"]["dataShape"],
        ai_purpose=sample["facets"]["aiPurpose"][0],
        field_schema=json.dumps(schema, ensure_ascii=False),
        language=["中文"],
        data_format=["JSON"],
        data_level="一般数据",
        data_size=metadata.get("数据规模", ""),
        update_frequency=metadata.get("更新频率", ""),
        sample_data=json.dumps(rows, ensure_ascii=False),
        version_notes=IMPORT_NOTE,
    )
    validate_submission(data)
    return data.model_dump(by_alias=True)


def sample_resources() -> list[DemoResource]:
    """按依赖顺序读取五个模块的 54 条样例，只保留有完整资料的当前版本。"""
    result = [
        DemoResource("dataset", item["id"], dataset_values(item))
        for item in fixture("datasets", "resources")
    ]
    for item in SimulatedModelProvider().list_models():
        # 模型供应方已按当前 ModelInput 校验过专属资料，再剔除目录展示字段。
        values = form_values(ModelInput, item)
        values.update(
            owner=item["owner"],
            category=item["category"],
            contact="模拟资料维护人",
            descriptionMarkdown=item["description"] + "\n\n> " + IMPORT_NOTE,
            endpoint="",
            usageExample="",
            versionNotes=IMPORT_NOTE,
            mode="simulation",
        )
        result.append(DemoResource("model", item["id"], values))
    for item in fixture("knowledge", "bases"):
        result.append(
            DemoResource(
                "knowledge",
                item["id"],
                {
                    "name": item["name"],
                    "summary": item["description"],
                    "description": item["description"],
                    "descriptionMarkdown": item["description"] + "\n\n> " + IMPORT_NOTE,
                    "owner": item["owner"],
                    "contact": "模拟资料维护人",
                    "category": item["category"],
                    "knowledgeType": "document",
                    "dataLevel": "一般数据",
                    "tags": [],
                    "version": "v1.0",
                    "versionNotes": IMPORT_NOTE,
                },
                is_public=item["visibility"] == "集团公开",
            )
        )
    for item in seed_tools():
        values = form_values(ToolInput, item)
        values.update(
            owner=item["owner"],
            contact="模拟资料维护人",
            category=item["category"],
            capabilities=item["capabilities"],
            descriptionMarkdown=item["descriptionMarkdown"] + "\n\n> " + IMPORT_NOTE,
            versionNotes=IMPORT_NOTE,
            runtimeMode="simulation",
        )
        result.append(DemoResource("tool", item["id"], values))
    for item in seed_applications():
        values = form_values(ApplicationDetails, {**item, "summary": item["description"]})
        values.update(contact="模拟资料维护人", versionNotes=IMPORT_NOTE)
        # 旧编号只用于定位这批样例；关联在提交时由仓储冻结为正式版本 UUID。
        for dependency in values["dependencies"]:
            dependency["resourceId"] = DemoResource(
                dependency["kind"], dependency["resourceId"], {}
            ).identifier()
        result.append(DemoResource("application", item["id"], values))
    return result


def main_projection(sample: DemoResource) -> dict:
    """转换概览中有独立类型的字段，版本表继续保留原表单的显示值。"""
    values = dict(sample.data)
    values["id"] = sample.identifier()
    if sample.kind == "tool":
        values["sourceType"] = (
            "self_developed" if values["sourceType"] == "自研工具" else "third_party"
        )
    if sample.kind == "dataset":
        size = values.get("dataSize", "").split()
        units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        if len(size) == 2 and size[1] in units:
            values["dataSizeBytes"] = int(Decimal(size[0]) * units[size[1]])
    return values
