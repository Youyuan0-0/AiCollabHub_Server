from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.modules.knowledge import service
from app.modules.knowledge.schemas import (
    ChunkQuery,
    CreateInput,
    IndexInput,
    RetrievalInput,
    UploadInput,
)

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge (mock)"])


@router.get("", response_model=None)
def bases() -> list[dict]:
    """查询固定知识库样例列表。"""
    return service.list_bases()


@router.post("", status_code=201, response_model=None)
def create(payload: CreateInput) -> dict:
    """校验创建参数并返回模拟知识库资料。"""
    return service.create(payload)


@router.get("/{base_id}/chunks", response_model=None)
def chunks(base_id: str, query: Annotated[ChunkQuery, Query()]) -> dict:
    """分页查询指定知识库的片段样例。"""
    return service.chunks(base_id, query)


@router.post("/{base_id}/documents", response_model=None)
def upload(base_id: str, payload: UploadInput) -> dict:
    """登记文件名和大小的模拟结果，不接收或解析文件正文。"""
    item = service.base(base_id)
    # 只接收文件名和大小以验证 DTO；不上传真实文件或调用文档解析。
    item["documents"] = [
        {
            "name": file.name,
            "type": file.name.split(".")[-1].upper(),
            "size": f"{file.size / 1024:.1f} KB",
            "updatedAt": item["updatedAt"],
            "status": "parsing",
            "chunkCount": 0,
        }
        for file in payload.files
    ] + item["documents"]
    item["documentCount"] = len(item["documents"])
    item["status"] = "syncing"
    return item


@router.delete("/{base_id}/documents", response_model=None)
def delete_document(base_id: str, name: str) -> dict:
    """返回移除指定文档后的知识库样例，不持久化修改。"""
    item = service.base(base_id)
    item["documents"] = [doc for doc in item["documents"] if doc["name"] != name]
    item["documentCount"] = len(item["documents"])
    return item


@router.put("/{base_id}/settings", response_model=None)
def settings(base_id: str, payload: IndexInput) -> dict:
    """返回更新索引设置后的知识库模拟资料。"""
    return {**service.base(base_id), "indexConfig": payload.model_dump(by_alias=True)}


@router.post("/{base_id}/rebuild", response_model=None)
def rebuild(base_id: str) -> dict:
    """返回索引就绪的模拟结果，不启动真实索引任务。"""
    return {**service.base(base_id), "status": "ready"}


@router.post("/{base_id}/retrieval", response_model=None)
def retrieve(base_id: str, payload: RetrievalInput) -> list[dict]:
    """按检索参数返回知识库固定演示片段。"""
    return service.retrieve(base_id, payload)
