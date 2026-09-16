from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.common.schemas import CatalogQuery
from app.modules.models.service import list_resources

router = APIRouter(prefix="/models", tags=["models (mock)"])


@router.get("", response_model=None)
def resources(query: Annotated[CatalogQuery, Query()]) -> dict:
    """按目录条件分页查询模型样例。"""
    return list_resources(query)
