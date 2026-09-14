from fastapi import HTTPException

from app.modules.common.fixtures import fixture, paginate
from app.modules.common.service import receipt, timestamp
from app.modules.knowledge.schemas import ChunkQuery, CreateInput, RetrievalInput


def list_bases():
    return fixture("knowledge", "bases")


def base(base_id: str):
    item = next((item for item in list_bases() if item["id"] == base_id), None)
    if item is None:
        raise HTTPException(404, "sample_knowledge_base_not_found")
    return item


def create(payload: CreateInput):
    return {
        **payload.model_dump(by_alias=True),
        "id": receipt()["id"],
        "owner": "演示单位",
        "documentCount": 0,
        "chunkCount": 0,
        "documents": [],
        "tone": "blue",
        "status": "ready",
        "updatedAt": timestamp()[:10],
        "indexConfig": payload.index_config.model_dump(by_alias=True)
        if payload.index_config
        else fixture("knowledge", "index-defaults"),
    }


def chunks(base_id: str, query: ChunkQuery):
    base(base_id)
    items = [
        item
        for item in fixture("knowledge", "chunks").get(base_id, [])
        if (not query.documentName or item["documentName"] == query.documentName)
        and query.q.casefold() in item["text"].casefold()
    ]
    return paginate(items, query.page, query.pageSize)


def retrieve(base_id: str, payload: RetrievalInput):
    return [
        {
            "id": doc["name"],
            "title": doc["name"],
            "excerpt": "固定演示片段，未执行实际检索。",
            "score": max(0, 0.92 - index * 0.05),
            "location": f"第 {index + 1} 段",
        }
        for index, doc in enumerate(base(base_id)["documents"])
        if max(0, 0.92 - index * 0.05) >= payload.config.similarity_threshold
    ][: payload.config.top_k]
