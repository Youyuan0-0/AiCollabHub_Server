from __future__ import annotations

from fastapi import HTTPException

from app.modules.common.fixtures import fixture, paginate
from app.modules.common.service import receipt, timestamp
from app.modules.knowledge.schemas import ChunkQuery, CreateInput, RetrievalInput


def list_bases() -> list[dict]:
    """读取固定知识库样例的独立副本。"""
    return fixture("knowledge", "bases")


def base(base_id: str) -> dict:
    """按编号查找知识库样例，不存在时返回 404。"""
    item = next((item for item in list_bases() if item["id"] == base_id), None)
    if item is None:
        raise HTTPException(404, "sample_knowledge_base_not_found")
    return item


def create(payload: CreateInput) -> dict:
    """构造知识库创建回执，缺省索引配置使用固定样例。"""
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


def chunks(base_id: str, query: ChunkQuery) -> dict:
    """按文档名称和关键词筛选知识库片段样例并分页。"""
    base(base_id)
    items = [
        item
        for item in fixture("knowledge", "chunks").get(base_id, [])
        if (not query.document_name or item["documentName"] == query.document_name)
        and query.q.casefold() in item["text"].casefold()
    ]
    return paginate(items, query.page, query.page_size)


def retrieve(base_id: str, payload: RetrievalInput) -> list[dict]:
    """按检索阈值返回固定演示片段，不执行真实向量检索。"""
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
