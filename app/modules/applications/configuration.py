"""有限的表单/管理列配置。字段结构稳定，不能由浏览器新增任意业务字段。"""

from fastapi import HTTPException
from pydantic import Field, model_validator

from app.modules.applications.publication_schemas import APPLICATION_CATEGORIES, ApplicationPayload

FIELD_LABELS = {
    "name": "应用名称",
    "englishName": "英文名称",
    "version": "版本",
    "category": "应用分类",
    "owner": "所属单位",
    "contact": "联系人",
    "summary": "应用简介",
    "tags": "关键词",
    "versionNotes": "版本说明",
    "capabilities": "能力标签",
    "baseModel": "底层模型",
    "descriptionMarkdown": "详细说明",
    "technicalStack": "技术栈",
    "deployment": "部署方式",
    "triggerType": "触发方式",
    "communicationProtocol": "通信协议",
    "inputSchema": "输入格式",
    "outputSchema": "输出格式",
    "dependencies": "关联资源",
    "tutorials": "使用教程",
    "configOptions": "配置项",
    "usageLimits": "使用限制",
}
CORE_FIELDS = ("name", "version", "category", "owner", "summary")
MANAGEMENT_COLUMNS = {
    "englishName": "英文名称",
    "category": "分类",
    "owner": "所属单位",
    "version": "版本",
    "usage": "使用次数",
    "rating": "评分",
    "downloadCount": "下载量",
    "favoriteCount": "收藏数",
    "createdAt": "创建时间",
    "updatedAt": "更新时间",
}


def default_configuration():
    return {
        "revision": 1,
        "updatedAt": None,
        "requiredFields": list(CORE_FIELDS),
        "fieldHints": {},
        "managementColumns": ["category", "owner", "version", "usage", "updatedAt"],
    }


class SaveApplicationConfiguration(ApplicationPayload):
    revision: int = Field(ge=1)
    required_fields: list[str] = Field(max_length=len(FIELD_LABELS))
    field_hints: dict[str, str] = Field(default_factory=dict, max_length=len(FIELD_LABELS))
    management_columns: list[str] = Field(min_length=1, max_length=len(MANAGEMENT_COLUMNS))

    @model_validator(mode="after")
    def valid_configuration(self):
        if not set(CORE_FIELDS).issubset(self.required_fields):
            raise ValueError("应用名称、版本、分类、所属单位和简介必须保留为必填")
        if (
            set(self.required_fields) - FIELD_LABELS.keys()
            or set(self.field_hints) - FIELD_LABELS.keys()
        ):
            raise ValueError("包含未支持的表单字段")
        if set(self.management_columns) - MANAGEMENT_COLUMNS.keys():
            raise ValueError("包含未支持的管理列")
        if len(set(self.required_fields)) != len(self.required_fields) or len(
            set(self.management_columns)
        ) != len(self.management_columns):
            raise ValueError("配置项不能重复")
        if any(len(value) > 300 for value in self.field_hints.values()):
            raise ValueError("字段说明不能超过300字")
        self.field_hints = {
            key: value.strip() for key, value in self.field_hints.items() if value.strip()
        }
        return self


def configuration_view(configuration: dict, *, public=False):
    result = {
        "revision": configuration["revision"],
        "categories": list(APPLICATION_CATEGORIES),
        "fields": [
            {
                "key": key,
                "label": label,
                "required": key in configuration["requiredFields"],
                "locked": key in CORE_FIELDS,
                "helpText": configuration["fieldHints"].get(key, ""),
            }
            for key, label in FIELD_LABELS.items()
        ],
    }
    if not public:
        result.update(configuration)
        result["availableColumns"] = [
            {"key": key, "label": label} for key, label in MANAGEMENT_COLUMNS.items()
        ]
    return result


def validate_submission(data: dict, configuration: dict):
    missing = [FIELD_LABELS[key] for key in configuration["requiredFields"] if not data.get(key)]
    if missing:
        raise HTTPException(422, "请填写" + "、".join(missing))
    if data["category"] not in APPLICATION_CATEGORIES:
        raise HTTPException(422, "请选择有效的应用分类")
