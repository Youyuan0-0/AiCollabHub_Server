"""发布与运行共用 JSON Schema 方言和结构校验；不读取网络引用。"""

from __future__ import annotations

import json
from typing import NoReturn

from jsonschema import Draft7Validator, Draft201909Validator, Draft202012Validator, SchemaError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for


def parse_schema(value: str) -> tuple[dict, type[Validator]]:
    """校验 JSON Schema 对象及支持的方言，返回文档和验证器类型，不访问远程引用。"""

    def invalid_constant(_: str) -> NoReturn:
        """拒绝 JSON 中不符合标准的非有限数值字面量。"""
        raise ValueError("JSON Schema 不支持 NaN 或 Infinity")

    try:
        schema = json.loads(value, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError("请填写有效的 JSON Schema 对象，或暂时留空") from error
    if not isinstance(schema, dict):
        raise ValueError("JSON Schema 顶层需要是对象")
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise ValueError("JSON Schema 的 $schema 必须是方言地址字符串")
    validator = validator_for(schema, default=None) if "$schema" in schema else Draft202012Validator
    if validator not in (Draft7Validator, Draft201909Validator, Draft202012Validator):
        raise ValueError("暂不支持该 JSON Schema 方言，请使用 Draft 7、2019-09 或 2020-12")
    try:
        validator.check_schema(schema)
    except (SchemaError, RecursionError, TypeError) as error:
        raise ValueError(
            "JSON Schema 定义不正确，请检查 type、properties、required 等关键字"
        ) from error
    return schema, validator
