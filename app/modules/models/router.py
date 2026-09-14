from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.common.schemas import CatalogQuery
from app.modules.models.service import list_resources

router = APIRouter(prefix="/models", tags=["models (mock)"])


@router.get("")
def resources(query: Annotated[CatalogQuery, Query()]):
    return list_resources(query)
