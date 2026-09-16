from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.common.schemas import CatalogQuery
from app.modules.tools.service import list_resources

router = APIRouter(prefix="/tools", tags=["tools (mock)"])


@router.get("", response_model=None)
def resources(query: Annotated[CatalogQuery, Query()]) -> dict:
    """按目录条件分页查询工具样例。"""
    return list_resources(query)
