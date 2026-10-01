"""数据集分类的历史名称兼容；不推测未提供的用途或删除自定义历史值。"""

ALIASES = {
    "dataApplication": {"通识数据": "通识数据集", "智能试验": "智能实验"},
    "dataShape": {"时序": "时序数据"},
    "aiPurpose": {
        "预训练数据集": "预训练",
        "微调数据集": "后训练",
        "评测数据集": "评测",
    },
}


def classification_label(field: str, value: str) -> str:
    """将已知旧名称映射为当前分类，未知历史名称原样保留。"""
    return ALIASES.get(field, {}).get(value, value)


def normalize_classification(resource: dict) -> dict:
    """统一目录与详情分类的展示值，避免改名后旧资源无法使用新标签筛选。"""
    result = dict(resource)
    facets = {key: list(values) for key, values in (resource.get("facets") or {}).items()}
    for field in ALIASES:
        if field in result:
            value = result[field]
            result[field] = (
                list(dict.fromkeys(classification_label(field, item) for item in value))
                if isinstance(value, list)
                else classification_label(field, value)
            )
        if field in facets:
            facets[field] = list(
                dict.fromkeys(classification_label(field, item) for item in facets[field])
            )
    if "category" in result:
        result["category"] = classification_label("dataApplication", result["category"])
    if "facets" in resource:
        result["facets"] = facets
    return result
