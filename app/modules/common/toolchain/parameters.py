"""仅替换声明过的业务数值，不执行表达式、不猜测供应方参数名称。"""

from pydantic import JsonValue

CHUNK_PARAMETERS = {"${chunkSize}", "${chunkOverlap}"}


def parameter_references(value: JsonValue) -> set[str]:
    """校验完整占位符并收集引用；JSON 字段名必须由供应方明确提供。"""
    if isinstance(value, str) and "${" in value:
        if value not in CHUNK_PARAMETERS:
            raise ValueError("分段参数只支持完整的 ${chunkSize} 和 ${chunkOverlap} 占位符")
        return {value}
    if isinstance(value, dict):
        if any("${" in key for key in value):
            raise ValueError("供应方参数名称不能使用占位符")
        return set().union(*(parameter_references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(parameter_references(item) for item in value))
    return set()


def resolve_parameters(value: JsonValue, chunk_size: int, chunk_overlap: int) -> JsonValue:
    """生成独立请求参数，将用户输入保留为整数，避免字符串替换改变 JSON 类型。"""
    if isinstance(value, str):
        return {"${chunkSize}": chunk_size, "${chunkOverlap}": chunk_overlap}.get(value, value)
    if isinstance(value, dict):
        return {
            key: resolve_parameters(item, chunk_size, chunk_overlap) for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_parameters(item, chunk_size, chunk_overlap) for item in value]
    return value
