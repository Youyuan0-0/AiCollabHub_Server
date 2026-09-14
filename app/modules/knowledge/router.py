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


@router.get("")
def bases():
    return service.list_bases()


@router.post("", status_code=201)
def create(payload: CreateInput):
    return service.create(payload)


@router.get("/{base_id}/chunks")
def chunks(base_id: str, query: Annotated[ChunkQuery, Query()]):
    return service.chunks(base_id, query)


@router.post("/{base_id}/documents")
def upload(base_id: str, payload: UploadInput):
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


@router.delete("/{base_id}/documents")
def delete_document(base_id: str, name: str):
    item = service.base(base_id)
    item["documents"] = [doc for doc in item["documents"] if doc["name"] != name]
    item["documentCount"] = len(item["documents"])
    return item


@router.put("/{base_id}/settings")
def settings(base_id: str, payload: IndexInput):
    return {**service.base(base_id), "indexConfig": payload.model_dump(by_alias=True)}


@router.post("/{base_id}/rebuild")
def rebuild(base_id: str):
    return {**service.base(base_id), "status": "ready"}


@router.post("/{base_id}/retrieval")
def retrieve(base_id: str, payload: RetrievalInput):
    return service.retrieve(base_id, payload)
