from __future__ import annotations

from app.modules.common.catalog import list_samples
from app.modules.common.schemas import CatalogQuery


def list_resources(query: CatalogQuery) -> dict:
    """筛选模型固定样例并返回分页结果。"""
    # 当前只读取样例；后续真实元数据接入在本模块实现。
    return list_samples("models", query)
