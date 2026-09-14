from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.applications import service
from app.modules.applications.schemas import ChatInput
from app.modules.common.schemas import CatalogQuery

router = APIRouter(tags=["applications (mock)"])


@router.get("/applications")
def resources(query: Annotated[CatalogQuery, Query()]):
    return service.list_resources(query)


@router.get("/conversations")
def conversations():
    return service.conversations()


@router.post("/conversations")
def ask(payload: ChatInput):
    return service.answer(payload)


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str):
    return service.sample_conversation(conversation_id)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str):
    # 仅验证删除请求与 204 响应，不改变固定演示数据。
    return None
