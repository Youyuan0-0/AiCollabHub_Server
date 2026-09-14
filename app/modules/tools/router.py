from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.common.schemas import CatalogQuery
from app.modules.tools.service import list_resources

router = APIRouter(prefix="/tools", tags=["tools (mock)"])


@router.get("")
def resources(query: Annotated[CatalogQuery, Query()]):
    return list_resources(query)
