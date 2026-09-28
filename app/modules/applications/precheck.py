"""表单预检查不保存业务资料；最终提交仍执行完整服务端校验。"""

from __future__ import annotations

import json

from pydantic import Field, ValidationError, field_validator

from app.modules.applications.configuration import FIELD_LABELS, effective_configuration
from app.modules.applications.discovery import analyze
from app.modules.applications.publication_schemas import (
    APPLICATION_CATEGORIES,
    ApplicationDetails,
    ApplicationPayload,
)
from app.modules.common.content_validation import sensitive_errors


class ApplicationPrecheck(ApplicationPayload):
    """发布表单的有限预检资料，保留半成品字段用于逐项提示。"""

    data: dict = Field(max_length=len(FIELD_LABELS))
    security_level: str = Field(default="internal", max_length=30)

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value: dict) -> dict:
        """拒绝未知字段和过长表单，控制预检输入范围。"""
        if value.keys() - FIELD_LABELS.keys():
            raise ValueError("包含未知的应用字段")
        if len(json.dumps(value, ensure_ascii=False)) > 150000:
            raise ValueError("应用资料过长")
        return value


def precheck(payload: ApplicationPrecheck, configuration: dict, policy: dict) -> dict:
    """检查尚未完成的发布表单，返回字段提示及本次使用的规则修订号。

    预检不保存资料，也不构成最终提交凭据；配置和策略可能在预检之后变化，
    提交入口仍须重新执行完整服务端校验。分析只接收类型可识别且未命中敏感词
    的部分，避免半成品字段导致元数据分析异常或把拦截内容带入建议。
    """
    configuration = effective_configuration(configuration)
    errors = {}
    data = payload.data
    try:
        ApplicationDetails.model_validate(data)
    except ValidationError as exception:
        for error in exception.errors(include_input=False, include_context=False):
            key = str(error["loc"][0])
            message = (
                error["msg"].removeprefix("Value error, ")
                if key in ("inputSchema", "outputSchema") and error["type"] == "value_error"
                else f"{FIELD_LABELS.get(key, key)}格式不正确，请检查内容和长度"
            )
            errors.setdefault(key, message)
        if "tutorials" in errors:
            errors["tutorials"] = "请补全教程标题，并填写有效链接或上传文件；不需要的空白教程可移除"
    for key in configuration["requiredFields"]:
        value = data.get(key)
        if not value or (isinstance(value, str) and not value.strip()):
            errors[key] = f"请填写{FIELD_LABELS[key]}"
    if data.get("category") and data["category"] not in APPLICATION_CATEGORIES:
        errors["category"] = "请选择有效的应用分类"
    blocked = sensitive_errors(data, policy["sensitiveTerms"])
    errors.update(blocked)
    if payload.security_level == "confidential":
        errors["securityLevel"] = "当前发布通道不接收涉密资料，请走专用审批通道"
    elif payload.security_level not in ("internal", "public"):
        errors["securityLevel"] = "请选择有效的密级"
    # 半成品字段可能不是期望类型，分类分析仅接收规范化后可识别的部分。
    safe = {
        key: value for key, value in data.items() if isinstance(value, str) and key not in blocked
    }
    for key in ("tags", "capabilities", "dependencies"):
        safe[key] = data.get(key) if isinstance(data.get(key), list) and key not in blocked else []
    return {
        "fieldErrors": errors,
        "configurationRevision": configuration["revision"],
        "policyRevision": policy["revision"],
        "discovery": analyze(safe),
    }
