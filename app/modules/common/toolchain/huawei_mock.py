"""华为已交付查询接口的本地示例，不访问真实服务。"""

import json

import httpx

from app.modules.common.toolchain.huawei import AGENT_DETAIL, CATALOG_PATHS, DOCUMENT_LIST


class HuaweiToolchainMock:
    """提供与华为文档相同的鉴权、分页及详情响应，用于接入演示。"""

    def __init__(self) -> None:
        self.rows = {
            "applications": [
                {
                    "id": "hw-agent-1",
                    "name": "产品咨询助手",
                    "description": "产品问题与服务咨询",
                    "version": "1.2",
                    "is_publish": True,
                    "engine": "agent",
                    "status": 1,
                }
            ],
            "datasets": [
                {
                    "id": "hw-dataset-1",
                    "name": "产品问答数据集",
                    "description": "产品问题与答案元数据",
                    "version": "2026.10",
                    "record_count": 1200,
                    "file_size": 64000,
                    "data_format": "json",
                    "status": 1,
                }
            ],
            "knowledge": [
                {
                    "id": "hw-knowledge-1",
                    "name": "产品资料库",
                    "description": "产品手册与服务文档",
                    "data": {"type": "chunker"},
                    "embedding_model_id": "hw-embedding-1",
                }
            ],
            "models": [
                {
                    "id": "hw-model-1",
                    "name": "业务语言模型服务",
                    "protocol": "openai",
                    "model_type": "llm",
                    "valid_code": 1,
                    "status": 1,
                    "config": {"api_key": "mock-hidden-key"},
                }
            ],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """仅响应文档中存在的 POST 接口，模拟数据不能生成真实下载地址。"""
        if request.method != "POST" or not request.headers.get("api-key"):
            return httpx.Response(401)
        payload = json.loads(request.content)
        if request.url.path == AGENT_DETAIL:
            row = next(
                (row for row in self.rows["applications"] if row["id"] == payload.get("id")), None
            )
            return httpx.Response(200, json={"code": 200 if row else 404, "data": row})
        if request.url.path == DOCUMENT_LIST:
            rows = [
                {
                    "id": "hw-doc-1",
                    "knowledge_id": "hw-knowledge-1",
                    "name": "产品手册.pdf",
                    "status": 4,
                    "segment_count": 32,
                    "file_size": 64000,
                    "file_id": "external-file-1",
                }
            ]
            if payload.get("knowledge_id") != "hw-knowledge-1":
                rows = []
        else:
            module = next(
                (module for module, path in CATALOG_PATHS.items() if path == request.url.path), None
            )
            if module is None:
                return httpx.Response(404)
            rows = self.rows[module]
        page, size = payload.get("page", 1), payload.get("size", 10)
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "success",
                "data": {
                    "total": len(rows),
                    "data": rows[(page - 1) * size : page * size],
                },
            },
        )
