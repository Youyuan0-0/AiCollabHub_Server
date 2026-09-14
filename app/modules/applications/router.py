from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.applications import service
from app.modules.common.schemas import CatalogQuery

router = APIRouter(tags=["applications (mock)"])


@router.get("/applications")
def resources(query: Annotated[CatalogQuery, Query()]):
    return service.list_resources(query)


# 会话由 app.api.conversations 统一提供数据库历史与多轮接口，此模块仅保留应用目录。
