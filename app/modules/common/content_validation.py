"""发布表单通用文本规则，词表由业务模块注入。"""

import json


def sensitive_errors(data: dict, terms: list[str]) -> dict[str, str]:
    """检查各字段是否命中拦截词，返回字段与错误说明的映射。"""
    return {
        key: "内容命中发布拦截词，请修改后提交"
        for key, value in data.items()
        if any(
            term.casefold() in json.dumps(value, ensure_ascii=False).casefold() for term in terms
        )
    }
