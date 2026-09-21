"""Knowledge Engine 协议适配，依据 20260917 指南，严格保留不支持能力的边界。"""

import json
from pathlib import Path

from fastapi import HTTPException

from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import list_result, object_result, segment
from app.modules.common.toolchain.parameters import resolve_parameters
from app.modules.knowledge.engine_schemas import CreateInput, IndexInput, RetrievalInput

BASE = "/knowledge-engine/api/v1"


class KnowledgeEngineProvider:
    """实现文档协议；未知索引和检索参数须配置经确认的映射，不静默忽略。"""

    def __init__(self, client: PopClient) -> None:
        self.client = client
        self.mode = client.mode
        self.configuration: dict = {}
        if client.settings.toolchain_knowledge_data is not None:
            self.configuration = client.settings.toolchain_knowledge_data
        elif client.settings.toolchain_knowledge_configuration:
            self.configuration = object_result(
                json.loads(
                    Path(
                        client.settings.toolchain_knowledge_configuration,
                    ).read_text(encoding="utf-8")
                )
            )

    def index_options(self, config: IndexInput) -> dict:
        """匹配已确认参数，动态分段按用户输入替换，保留字符数值及 JSON 数字类型。"""
        if self.mode == "mock":
            return {}
        values = config.model_dump(by_alias=True)
        for profile in self.configuration.get("indexProfiles", []):
            fields = ("mode", "embeddingModel", "chunkMode")
            if not profile.get("dynamicChunking", False):
                fields += ("chunkSize", "chunkOverlap")
            if profile.get("confirmed") is True and all(
                profile.get("match", {}).get(field) == values[field] for field in fields
            ):
                options = object_result(profile.get("pipelineOptions"))
                return object_result(
                    resolve_parameters(options, config.chunk_size, config.chunk_overlap)
                )
        raise HTTPException(422, "所选索引、模型和分段配置尚无供应方确认的参数映射，请联系管理员")

    def options(self) -> dict:
        """向业务页面提供已确认的业务选项，不暴露供应方参数、地址或凭据。"""
        return {
            "mode": self.mode,
            "indexProfiles": [
                {
                    "match": profile["match"],
                    "dynamicChunking": profile.get("dynamicChunking", False),
                }
                for profile in self.configuration.get("indexProfiles", [])
                if profile.get("confirmed") is True
            ],
        }

    def create(self, payload: CreateInput, unique_name: str, tenant_id: str | None = None) -> str:
        """创建 FILE 知识库并验证外部 UUID；技术名称隔离平台不同资源。"""
        options = self.index_options(payload.index_config or IndexInput())
        data = object_result(
            self.client.request(
                "POST",
                BASE + "/datasets",
                action="PostKnowledgeEngineApiV1Datasets",
                body={
                    "name": unique_name,
                    "kind": "FILE",
                    "description": payload.description or payload.summary,
                    **({"tenantId": tenant_id} if tenant_id else {}),
                    **({"pipelineOptions": options} if options else {}),
                },
            )
        )
        if not isinstance(data.get("id"), str) or not data["id"]:
            raise HTTPException(502, "知识引擎未返回有效知识库编号")
        return data["id"]

    def add(self, identifier: str, files: list[dict]) -> None:
        """登记服务端提供的文件地址；状态须另行查询，不能直接标记完成。"""
        data = object_result(
            self.client.request(
                "POST",
                BASE + "/add",
                action="PostKnowledgeEngineApiV1Add",
                body={
                    "datasetId": identifier,
                    "dataUrls": [item["url"] for item in files],
                    "fileName": [item["name"] for item in files],
                    "runInBackground": True,
                },
            )
        )
        if data.get("dataset_id") != identifier:
            raise HTTPException(502, "知识引擎登记结果的知识库编号不匹配")

    def documents(self, identifier: str) -> list[dict]:
        """按 offset 读取所有文档，验证每个返回项所属知识库。"""
        documents = []
        for offset in range(0, 10000, 100):
            data = object_result(
                self.client.request(
                    "GET",
                    BASE + "/datasets/" + segment(identifier) + "/data",
                    action="GetKnowledgeEngineApiV1DatasetsDatasetIdData",
                    query={"offset": offset, "limit": 100},
                )
            )
            items = list_result(data.get("data"))
            for item in items:
                if item.get("dataset_id") != identifier or not isinstance(item.get("id"), str):
                    raise HTTPException(502, "知识引擎返回了不属于当前知识库的文档")
                states = object_result(item.get("pipeline_status", {}))
                state = object_result(states.get("cognify_pipeline", {})).get(identifier, "")
                status = self.state(state)
                documents.append(
                    {
                        "id": item["id"],
                        "name": item.get("name", ""),
                        "type": str(item.get("extension", "")).upper(),
                        "sizeBytes": item.get("data_size") or 0,
                        "size": f"{(item.get('data_size') or 0) / 1024:.1f} KB",
                        "updatedAt": item.get("updated_at") or item.get("created_at") or "",
                        "status": status,
                    }
                )
            if data.get("has_more") is False or len(documents) >= data.get("total", len(documents)):
                return documents
            if not items:
                raise HTTPException(502, "知识引擎文档分页不完整")
        raise HTTPException(502, "知识库文档数超过当前处理上限")

    @staticmethod
    def state(value: object) -> str:
        """未知或未启动状态保持处理中，不把空响应判为处理成功。"""
        state = str(value)
        if state.endswith("COMPLETED"):
            return "ready"
        if "FAILED" in state or "PERMANENT" in state:
            return "failed"
        return "parsing"

    def status(self, identifier: str) -> str:
        """查询 cognify 而非仅检查内容登记状态。"""
        data = object_result(
            self.client.request(
                "GET",
                BASE + "/datasets/status",
                action="GetKnowledgeEngineApiV1DatasetsStatus",
                query={"dataset": identifier, "pipeline_name": "cognify_pipeline", "detail": True},
            )
        )
        state = data.get(identifier, {})
        return self.state(state.get("status", "") if isinstance(state, dict) else state)

    def chunks(self, identifier: str, document: dict, offset: int, limit: int) -> dict:
        """读取指定文档的真实片段并核对来源，页码转换由平台负责。"""
        data = object_result(
            self.client.request(
                "GET",
                BASE
                + "/datasets/"
                + segment(identifier)
                + "/data/"
                + segment(document["id"])
                + "/chunks",
                action="GetKnowledgeEngineApiV1DatasetsDatasetIdDataDataIdChunks",
                query={"offset": offset, "limit": limit},
            )
        )
        items = []
        for item in list_result(data.get("chunks")):
            if item.get("document_id") != document["id"]:
                raise HTTPException(502, "知识引擎返回了其他文档的片段")
            text = item.get("text", "")
            items.append(
                {
                    "id": item.get("chunk_id"),
                    "documentName": document["name"],
                    "index": (item.get("chunk_index") or 0) + 1,
                    "text": text,
                    "characterCount": len(text),
                }
            )
        return {"items": items, "total": data.get("total", len(items))}

    def delete(self, identifier: str, document_id: str) -> None:
        """仅删除已验证属于当前知识库的文档，不删除整个知识库。"""
        self.client.request(
            "DELETE",
            BASE + "/datasets/" + segment(identifier) + "/data/" + segment(document_id),
            action="DeleteKnowledgeEngineApiV1DatasetsDatasetIdDataDataId",
        )

    def retrieve(
        self, identifier: str, documents: list[dict], payload: RetrievalInput
    ) -> list[dict]:
        """在展示前严格过滤库及文档归属，缺失分数保留 null，不伪造相似度。"""
        config = payload.config
        extra = {}
        if self.mode == "live":
            if self.configuration.get("retrievalVerified") is not True:
                raise HTTPException(422, "知识库过滤与分数返回尚未完成真实接口验证，请联系管理员")
            values = config.model_dump(by_alias=True)
            match = next(
                (
                    profile
                    for profile in self.configuration.get("retrievalProfiles", [])
                    if profile.get("confirmed") is True
                    and all(
                        profile.get("match", {}).get(key) == values[key]
                        for key in ("reranking", "semanticSearch", "keywordSearch")
                    )
                ),
                None,
            )
            if match is None:
                raise HTTPException(422, "所选重排序、语义及关键词检索组合尚未确认，请联系管理员")
            extra = object_result(match.get("retrieverSpecificConfig", {}))
        result = self.client.request(
            "POST",
            BASE + "/search",
            action="PostKnowledgeEngineApiV1Search",
            body={
                "dataset_ids": [identifier],
                "query": payload.query,
                "search_type": "CHUNKS",
                "top_k": config.top_k,
                "score_threshold": config.similarity_threshold,
                "include_file_location": True,
                **({"retriever_specific_config": extra} if extra else {}),
            },
        )
        allowed = {item["id"]: item for item in documents if item.get("status") == "ready"}
        items = []
        for group in list_result(result):
            if group.get("dataset_id") != identifier:
                if self.mode == "live":
                    raise HTTPException(502, "知识引擎未正确限定知识库，无法确认检索结果")
                continue
            for item in list_result(group.get("search_result", [])):
                document = allowed.get(item.get("document_id"))
                if document is None:
                    if self.mode == "live":
                        raise HTTPException(502, "知识引擎返回了未登记或未就绪文档的结果")
                    continue
                score = item.get("score")
                if (
                    not isinstance(score, (float, int))
                    or isinstance(score, bool)
                    or not 0 <= score <= 1
                ):
                    if self.mode == "live":
                        raise HTTPException(502, "知识引擎缺少有效相似度分数，无法确认检索阈值")
                    score = None
                if score is not None and score < config.similarity_threshold:
                    continue
                items.append(
                    {
                        "id": item.get("id", ""),
                        "title": document["name"],
                        "excerpt": item.get("text", ""),
                        "score": score,
                        "location": f"第 {(item.get('chunk_index') or 0) + 1} 段",
                    }
                )
        return items[: config.top_k]
