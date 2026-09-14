from fastapi import HTTPException

from app.modules.applications.schemas import ChatInput
from app.modules.common.catalog import list_samples
from app.modules.common.schemas import CatalogQuery
from app.modules.common.service import receipt


def list_resources(query: CatalogQuery):
    return list_samples("applications", query)


def answer(payload: ChatInput):
    return {
        "id": receipt()["id"],
        "question": payload.question,
        "model": payload.model,
        "knowledgeBase": payload.knowledge_base,
        "text": (
            f"模拟回答：针对“{payload.question}”，"
            "可先核对工况、测点及数据完整性，再结合历史样例分析。"
        ),
        "sources": [{"title": "总体性能设计规范", "location": "第 4.2 节", "score": 0.92}],
    }


def sample_conversation(conversation_id: str):
    if conversation_id != "conversation-demo-1":
        raise HTTPException(404, "sample_conversation_not_found")
    result = answer(ChatInput(question="如何分析发动机试验数据？"))
    return {**result, "id": conversation_id}


def conversations():
    item = sample_conversation("conversation-demo-1")
    return [
        {"id": item["id"], "title": item["question"], "time": "示例会话", "model": item["model"]}
    ]
