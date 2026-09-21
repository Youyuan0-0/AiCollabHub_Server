"""依据文档响应结构提供开发传输；模拟处理不代表真实引擎解析或检索质量。"""

import json
from copy import deepcopy
from threading import RLock
from urllib.parse import unquote
from uuid import uuid4

import httpx


class DocumentedToolchainMock:
    """替代远程服务，业务适配器仍发送完整 HTTP 请求并解析供应方字段。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.bases: dict[str, dict] = {}
        self.files: dict[str, tuple[str, bytes]] = {}
        self.calls: list[tuple[str, str]] = []
        self.datasets = [
            {
                "code": "demo-dataset-001",
                "name": "工具链试验记录样例",
                "description": "用于验证成果导入的模拟数据集",
                "rowCount": 2,
                "originalFileSize": 120,
                "updatedAt": "2026-09-21T00:00:00Z",
                "tags": [{"name": "试验", "value": "试验"}],
                "subsets": [
                    {
                        "code": "default",
                        "features": [
                            {
                                "columnName": "item",
                                "columnType": "string",
                                "columnComment": "样例名称",
                            },
                            {
                                "columnName": "value",
                                "columnType": "double",
                                "columnComment": "样例数值",
                            },
                        ],
                        "splits": [{"code": "train"}],
                    }
                ],
            }
        ]

    def store_file(self, name: str, content: bytes) -> str:
        """保存实际收到的文件字节，用内部引用交给模拟知识处理服务。"""
        url = "https://toolchain.invalid/files/" + uuid4().hex
        with self.lock:
            self.files[url] = (name, content)
        return url

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """校验基本协议后分派；未知路径或方法返回失败而非假成功。"""
        with self.lock:
            self.calls.append((request.method, request.url.path))
            if len(self.calls) > 1000:
                del self.calls[:500]
            if not request.headers.get("authorization", "").startswith("acs "):
                return httpx.Response(403, json={"success": False, "data": None})
            if request.headers.get("x-acs-version") != "2025-11-10":
                return httpx.Response(400, json={"success": False, "data": None})
            body = json.loads(request.content) if request.content else {}
            if request.url.path.startswith("/knowledge-engine/"):
                for key in (
                    "dataUrls",
                    "fileName",
                    "runInBackground",
                    "pipelineOptions",
                    "dataset_ids",
                    "top_k",
                    "score_threshold",
                    "include_file_location",
                    "retriever_specific_config",
                ):
                    if isinstance(body.get(key), str):
                        body[key] = json.loads(body[key])
            data = self.dispatch(request, body)
            if data is NotImplemented:
                return httpx.Response(404, json={"success": False, "data": None})
            return httpx.Response(
                200,
                json={"success": True, "data": deepcopy(data), "traceId": "mock-" + uuid4().hex},
            )

    def dispatch(self, request: httpx.Request, body: dict) -> object:
        """实现本次需要的文档操作，按供应方结构保存及返回状态。"""
        path, method = unquote(request.url.path), request.method
        data_base = "/gatectl/data-engine/api/v2/datasets"
        if method == "GET" and path == data_base:
            page = int(request.url.params.get("pageNumber", 1))
            size = int(request.url.params.get("pageSize", 20))
            keyword = request.url.params.get("keyword", "")
            items = [item for item in self.datasets if keyword in item["name"]]
            return {"datasets": items[(page - 1) * size : page * size], "totalCount": len(items)}
        if method == "GET" and path.startswith(data_base + "/"):
            suffix = path[len(data_base) + 1 :].split("/")
            item = next((item for item in self.datasets if item["code"] == suffix[0]), None)
            if item is None:
                return NotImplemented
            if len(suffix) == 1:
                return item
            if suffix[1] == "viewer":
                return {
                    "schema": item["subsets"][0]["features"],
                    "status": "BUILT",
                    "data": [["样例一", 1.2], ["样例二", 2.4]],
                    "totalCount": 2,
                }
            if suffix[1] == "files":
                return {
                    "datasetFileInfos": [{"name": "demo.csv", "filepath": "demo.csv"}],
                    "totalCount": 1,
                }
        if method == "GET" and path.startswith("/gatectl/data-engine/api/v2/process/tasks/"):
            if path.endswith("/instances"):
                return {
                    "instances": [{"status": 3, "instanceCode": "demo-instance"}],
                    "totalCount": 1,
                }
            return {"taskId": "demo-task", "outputDatasetCode": "demo-dataset-001"}
        if method == "POST" and path == "/gatectl/agent/openapi/agent/pageQuery":
            items = [
                {
                    "agentCode": "demo-agent-001",
                    "agentName": "工具链报告助手",
                    "agentDesc": "用于验证智能体导入的模拟成果",
                    "workspaceCode": request.headers["x-sfm-workspacecode"],
                }
            ]
            return {"list": items if body.get("current") == 1 else [], "total": 1}
        if method == "POST" and path == "/gatectl/agent/openapi/agent/version/pageQuery":
            items = [
                {
                    "agentCode": "demo-agent-001",
                    "agentVersion": "20260921001",
                    "versionName": "演示版本一",
                    "agentName": "工具链报告助手",
                    "agentDefinition": "根据输入生成报告的演示智能体",
                    "agentStatus": "ONLINE",
                }
            ]
            if body.get("agentCode") != "demo-agent-001" or body.get("current") != 1:
                items = []
            return {"list": items, "total": len(items)}
        return self.knowledge(request, body)

    def knowledge(self, request: httpx.Request, body: dict) -> object:
        """模拟知识库注册、异步完成、文档片段及检索，不生成虚假的相似度。"""
        path = request.url.path.removeprefix("/knowledge-engine/api/v1")
        method = request.method
        if method == "POST" and path == "/datasets":
            identifier = str(uuid4())
            self.bases[identifier] = {"id": identifier, "name": body["name"], "documents": {}}
            return {"id": identifier, "name": body["name"], "kind": "FILE"}
        if method == "POST" and path == "/add":
            base = self.bases.get(body.get("datasetId"))
            if base is None:
                return NotImplemented
            ids = []
            for url, name in zip(body.get("dataUrls", []), body.get("fileName", []), strict=True):
                if url not in self.files:
                    return NotImplemented
                _, content = self.files[url]
                identifier = str(uuid4())
                is_text = name.lower().endswith((".txt", ".md", ".markdown"))
                text = (
                    content.decode("utf-8", errors="replace")
                    if is_text
                    else f"模拟解析内容：{name}"
                )
                base["documents"][identifier] = {
                    "id": identifier,
                    "name": name,
                    "extension": name.rsplit(".", 1)[-1],
                    "dataset_id": base["id"],
                    "data_size": len(content),
                    "_text": text,
                    "pipeline_status": {
                        "cognify_pipeline": {base["id"]: "DATA_ITEM_PROCESSING_STARTED"}
                    },
                }
                ids.append(identifier)
            return {"dataset_id": base["id"], "data_ids": ids, "new_count": len(ids)}
        if method == "GET" and path == "/datasets/status":
            identifier = request.url.params.get("dataset")
            base = self.bases.get(identifier)
            if base is None:
                return NotImplemented
            for item in base["documents"].values():
                item["pipeline_status"]["cognify_pipeline"][identifier] = (
                    "DATA_ITEM_PROCESSING_COMPLETED"
                )
            return {
                identifier: {
                    "status": "DATASET_PROCESSING_COMPLETED",
                    "progress": {"percentage": 100},
                }
            }
        if method == "POST" and path == "/search":
            results = []
            for identifier in body.get("dataset_ids", []):
                base = self.bases.get(identifier)
                if base is None:
                    continue
                chunks = [
                    {
                        "id": item["id"] + "-0",
                        "document_id": item["id"],
                        "text": item["_text"][:2000],
                        "chunk_index": 0,
                    }
                    for item in base["documents"].values()
                    if item["pipeline_status"]["cognify_pipeline"][identifier].endswith("COMPLETED")
                ]
                results.append(
                    {"dataset_id": identifier, "search_result": chunks[: body.get("top_k", 6)]}
                )
            return results
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "datasets" and parts[2] == "data":
            base = self.bases.get(parts[1])
            if base is None:
                return NotImplemented
            if method == "GET" and len(parts) == 3:
                items = [
                    {key: value for key, value in item.items() if not key.startswith("_")}
                    for item in base["documents"].values()
                ]
                offset, limit = (
                    int(request.url.params.get("offset", 0)),
                    int(request.url.params.get("limit", 100)),
                )
                return {
                    "data": items[offset : offset + limit],
                    "total": len(items),
                    "has_more": offset + limit < len(items),
                }
            item = base["documents"].get(parts[3]) if len(parts) >= 4 else None
            if item is None:
                return NotImplemented
            if method == "DELETE" and len(parts) == 4:
                del base["documents"][parts[3]]
                return None
            if method == "GET" and parts[-1] == "chunks":
                offset = int(request.url.params.get("offset", 0))
                chunks = [
                    {
                        "chunk_id": item["id"] + "-0",
                        "text": item["_text"][:2000],
                        "chunk_index": 0,
                        "document_id": item["id"],
                        "source_file_name": item["name"],
                    }
                ]
                return {"chunks": chunks if offset == 0 else [], "total": 1}
        return NotImplemented
