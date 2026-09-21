"""任务表第 30—38 行的模型专属字段，不套用工具或应用的必填规则。"""

import json
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from app.modules.common.access_schemas import StrictPayload

MODEL_FIELDS = {
    "name": "模型名称",
    "englishName": "英文名称",
    "version": "版本号",
    "summary": "模型简介",
    "modelStatus": "模型状态",
    "tags": "关键词",
    "taskType": "任务类型",
    "architecture": "模型架构",
    "parameters": "参数量",
    "contextLength": "上下文长度",
    "languages": "支持语言",
    "inputModalities": "输入模态",
    "outputModalities": "输出模态",
    "capabilities": "能力标签",
    "submodels": "子模型列表",
    "framework": "模型框架",
    "modelFormat": "模型格式",
    "precision": "推理精度",
    "configJson": "模型配置文件（config.json）",
    "license": "License",
    "serviceCapabilities": "支持体验/训练/评测/部署",
    "benchmarkScore": "基准测评得分",
    "domainScore": "领域测评得分",
    "inferenceSpeed": "推理速度",
    "gpuMemory": "显存要求",
    "endpoint": "API 端点",
    "deploymentDocument": "部署文档",
    "usageExample": "使用示例",
    "sourceType": "模型来源",
    "trainingDatasetIds": "训练数据集 ID",
    "securityLevel": "安全等级",
}
MODEL_REQUIRED = (
    "name",
    "version",
    "summary",
    "framework",
    "modelFormat",
    "precision",
    "configJson",
    "license",
    "serviceCapabilities",
)


class ModelInput(StrictPayload):
    """模型业务输入，编号、归属和统计由服务器生成。"""

    name: str = Field(default="", max_length=200)
    english_name: str = Field(default="", max_length=200)
    version: str = Field(default="v1.0", max_length=80)
    summary: str = Field(default="", max_length=2000)
    model_status: Literal["NEW", "合集", "推荐", "稳定", "弃用"] = "NEW"
    tags: list[str] = Field(default_factory=list, max_length=30)
    task_type: str = ""
    architecture: str = ""
    parameters: str = ""
    context_length: str = ""
    languages: list[str] = Field(default_factory=list)
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    submodels: list[str] = Field(default_factory=list)
    framework: str = ""
    model_format: str = ""
    precision: str = ""
    config_json: str = Field(default="", max_length=100000)
    license: str = ""
    service_capabilities: list[Literal["体验", "训练", "评测", "部署"]] = Field(
        default_factory=list
    )
    benchmark_score: str = ""
    domain_score: str = ""
    inference_speed: str = ""
    gpu_memory: str = ""
    endpoint: str = Field(default="", max_length=2000)
    deployment_document: str = ""
    usage_example: str = Field(default="", max_length=10000)


class SaveModel(StrictPayload):
    """允许暂存不完整草稿，提交使用模型专属校验。"""

    data: ModelInput
    submit: bool = False
    revision: int | None = Field(default=None, ge=1)


def validate_model(data: ModelInput) -> None:
    """提交时校验模型必填与配置对象，不把 API 地址设为必填。"""
    values = data.model_dump(by_alias=True)
    missing = [MODEL_FIELDS[key] for key in MODEL_REQUIRED if not values[key]]
    if missing:
        raise HTTPException(422, "请填写：" + "、".join(missing))
    try:
        config = json.loads(data.config_json)
        json.dumps(config, allow_nan=False)
        if not isinstance(config, dict):
            raise ValueError
    except (ValueError, TypeError, RecursionError) as error:
        raise HTTPException(422, "config.json 必须是 JSON 对象") from error
