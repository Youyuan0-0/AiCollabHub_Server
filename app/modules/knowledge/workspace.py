"""知识库平台管理状态与外部知识引擎边界，模拟阶段不解析真实文件。"""

from copy import deepcopy
from time import perf_counter
from typing import Protocol

from fastapi import HTTPException, Request

from app.modules.common.fixtures import fixture
from app.modules.common.identity import Actor, can_manage
from app.modules.common.resource_workspace import ResourceWorkspace
from app.modules.common.service import timestamp
from app.modules.knowledge.engine_schemas import (
    CreateInput,
    IndexInput,
    RetrievalInput,
    UploadInput,
)


class KnowledgeProvider(Protocol):
    """实际解析、索引和召回由知识引擎提供。"""

    def documents(self, payload: UploadInput) -> list[dict]:
        """提交文档并返回供应方处理状态。"""
        ...

    def retrieve(self, base: dict, payload: RetrievalInput) -> list[dict]:
        """按独立检索开关和阈值返回片段。"""
        ...


class SimulatedKnowledgeProvider:
    """只返回显式模拟状态和片段，不实现底层解析与向量算法。"""

    def documents(self, payload: UploadInput) -> list[dict]:
        """以文件元信息模拟已完成状态，大小保留数值以便汇总。"""
        return [
            {
                "name": file.name,
                "type": file.name.rsplit(".", 1)[-1].upper(),
                "size": f"{file.size / 1024:.1f} KB",
                "sizeBytes": file.size,
                "updatedAt": timestamp(),
                "status": "ready",
                "chunkCount": 1,
            }
            for file in payload.files
        ]

    def retrieve(self, base: dict, payload: RetrievalInput) -> list[dict]:
        """演练请求开关传递，结果不代表真实知识召回。"""
        return [
            {
                "id": doc["name"],
                "title": doc["name"],
                "excerpt": "模拟片段：" + doc["name"],
                "score": max(0, 0.92 - index * 0.05),
                "location": f"第 {index + 1} 段",
            }
            for index, doc in enumerate(base["documents"])
            if max(0, 0.92 - index * 0.05) >= payload.config.similarity_threshold
        ][: payload.config.top_k]


def create_knowledge_provider() -> KnowledgeProvider:
    """真实知识引擎的统一装配替换点。"""
    return SimulatedKnowledgeProvider()


def knowledge_seeds() -> list[dict]:
    """把旧知识库样例映射为当前字段，后台密级不下发普通界面。"""
    items = fixture("knowledge", "bases")
    for item in items:
        total = 0
        for doc in item["documents"]:
            number, unit = doc["size"].split()[:2]
            doc["sizeBytes"] = int(
                float(number) * {"KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(unit, 1)
            )
            total += doc["sizeBytes"]
        item.update(
            summary=item["description"],
            totalSize=total,
            uploadedAt=item["updatedAt"],
            indexConfig=IndexInput().model_dump(by_alias=True),
            dataLevel="一般数据",
            contact="模拟维护人",
            version="v1.0",
            kind="knowledge",
        )
    return items


class KnowledgeWorkspace(ResourceWorkspace):
    """复用资源审核授权，保留文档、分段和索引配置专属流程。"""

    def __init__(self, provider: KnowledgeProvider) -> None:
        super().__init__("knowledge", knowledge_seeds())
        self.provider = provider

    def retrieve(self, item: dict, payload: RetrievalInput) -> list[dict]:
        """调用当前知识供应方；子类只替换引擎协议，不重复统计逻辑。"""
        return self.provider.retrieve(item, payload)

    def search(self, identifier: str, actor: Actor, payload: RetrievalInput) -> list[dict]:
        """鉴权后统计完整召回请求；失败也计入，未授权请求不污染业务指标。"""
        item = self.resource(identifier, actor)
        if item["status"] != "ready":
            raise HTTPException(409, "知识库尚未处理完成，请先刷新处理状态")
        started, succeeded = perf_counter(), False
        try:
            result = self.retrieve(item, payload)
            succeeded = True
            return result
        finally:
            with self.lock:
                current = self.resources[identifier]
                count = current.get("retrievalCount", 0) + 1
                successes = current.get("retrievalSuccessCount", 0) + int(succeeded)
                total = current.get("retrievalTotalMs", 0) + (perf_counter() - started) * 1000
                current.update(
                    retrievalCount=count,
                    retrievalSuccessCount=successes,
                    retrievalTotalMs=total,
                    averageRetrievalMs=total / count,
                    retrievalSuccessRate=100 * successes / count,
                )

    def create(self, payload: CreateInput, actor: Actor, identity: dict) -> dict:
        """创建仅本人可见的知识库草稿，管理员分级后可审核公开。"""
        data = {
            **payload.model_dump(by_alias=True),
            **identity,
            "indexConfig": (payload.index_config or IndexInput()).model_dump(by_alias=True),
            "documents": [],
            "documentCount": 0,
            "chunkCount": 0,
            "totalSize": 0,
            "uploadedAt": None,
            "status": "ready",
            "tone": "blue",
            "version": "v1.0",
        }
        record = self.save(data, actor)
        item = {
            **data,
            "id": record["resourceId"],
            "kind": "knowledge",
            "publicationId": record["id"],
            "createdBy": actor.id,
            "organizationId": actor.organization_id,
            "revision": 1,
            "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"],
            "versions": [],
            "listingStatus": "unlisted",
            "visibility": {"scope": "all", "targets": [], "revision": 1},
        }
        with self.lock:
            self.resources[item["id"]] = item
        return deepcopy(item)

    def edit(self, identifier: str, actor: Actor, changes: dict) -> dict:
        """只有维护者或所属管理员可改文档和参数，更新留下版本快照。"""
        with self.lock:
            item = self.resource(identifier, actor)
            if item.get("createdBy") != actor.id and not can_manage(actor, item):
                raise HTTPException(403, "只有维护者可以管理知识库")
            publication = self.records.get(item.get("publicationId"), {})
            if publication.get("status") == "pending":
                raise HTTPException(409, "审核中的知识库不可修改")
            item.update(deepcopy(changes))
            item["revision"] += 1
            item["updatedAt"] = timestamp()
            item["version"] = f"v{item['revision']}.0"
            item["versions"].append(
                {
                    "version": item["version"],
                    "releasedAt": item["updatedAt"],
                    "note": "更新文档或索引配置",
                }
            )
            self.resources[identifier] = item
            return deepcopy(item)


def knowledge_workspace(request: Request) -> KnowledgeWorkspace:
    """返回实例隔离知识库状态。"""
    return request.app.state.knowledge_workspace


def public_knowledge(item: dict, actor: Actor) -> dict:
    """仅返回用户可展示字段，密级和开放范围保持后台专属。"""
    result = {
        key: value
        for key, value in item.items()
        if key
        not in (
            "visibility",
            "dataLevel",
            "organizationId",
            "createdBy",
            "contact",
            "versions",
        )
    }
    result["canManage"] = item.get("createdBy") == actor.id or can_manage(actor, item)
    return result
