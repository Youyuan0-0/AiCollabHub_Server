"""华为平台查询协议；只调用已交付接口，不暴露供应方配置及密钥。"""

import json
from collections.abc import Callable
from threading import BoundedSemaphore

import httpx
from fastapi import HTTPException

from app.core.config import Settings
from app.modules.common.toolchain.contracts import fingerprint, list_result, object_result
from app.modules.common.toolchain.network import http_options

CATALOG_PATHS = {
    "applications": "/api/agent/manager/query",
    "datasets": "/api/dataset/collection/list",
    "knowledge": "/api/knowledge/manager/list",
    "models": "/api/provider/manager/query",
}
AGENT_DETAIL = "/api/agent/manager/detail"
DOCUMENT_LIST = "/api/knowledge/manager/doc/list"


class HuaweiClient:
    """使用平台 API Key 发送受限查询；模拟模式强制使用本地传输。"""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.mode = settings.toolchain_mode
        self.workspace = ""
        if self.mode == "live" and not (settings.toolchain_pop_url and settings.toolchain_api_key):
            raise ValueError("华为真实查询需要服务地址和 API Key")
        if self.mode == "mock" and transport is None:
            raise ValueError("华为模拟查询必须提供本地传输")
        self.http = httpx.Client(
            base_url=str(settings.toolchain_pop_url or "https://huawei.invalid").rstrip("/"),
            timeout=settings.toolchain_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            **http_options(
                settings.toolchain_proxy_url if transport is None else None,
                settings.toolchain_ca_certificate,
            ),
        )
        self._limiter = BoundedSemaphore(8)

    def close(self) -> None:
        """释放查询连接池。"""
        self.http.close()

    def request(self, path: str, body: dict) -> object:
        """同时校验 HTTP 状态与整数业务状态，不透传供应方原始错误。"""
        if self.mode == "disabled":
            raise HTTPException(503, "工具链尚未配置")
        if path not in (*CATALOG_PATHS.values(), AGENT_DETAIL, DOCUMENT_LIST):
            raise ValueError("未支持的华为查询接口")
        if not self._limiter.acquire(blocking=False):
            raise HTTPException(429, "工具链请求较多，请稍后重试")
        key = self.settings.toolchain_api_key
        try:
            with self.http.stream(
                "POST",
                path,
                json=body,
                headers={"api-key": key.get_secret_value() if key else "mock-api-key"},
            ) as response:
                if response.status_code in (401, 403):
                    raise HTTPException(502, "华为工具链授权失败，请检查 API Key 和账号权限")
                if response.status_code != 200:
                    raise HTTPException(502, "华为工具链接口请求失败")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 8_000_000:
                        raise HTTPException(502, "华为工具链响应过大")
                    chunks.append(chunk)
                result = object_result(json.loads(b"".join(chunks)))
            if type(result.get("code")) is not int or result["code"] != 200 or "data" not in result:
                raise HTTPException(502, "华为工具链报告查询失败")
            return result["data"]
        except httpx.TimeoutException:
            raise HTTPException(504, "华为工具链查询超时") from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "华为工具链连接失败或返回格式不正确") from None
        finally:
            self._limiter.release()


def metadata(module: str, row: dict) -> dict:
    """仅保留可展示的元数据；config、engine_config 及知识库 data 不入目录快照。"""
    identifier, name = row.get("id"), row.get("name")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200:
        raise HTTPException(502, "华为资源编号无效")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise HTTPException(502, "华为资源名称无效")
    result = {"externalId": identifier, "name": name}
    fields = {
        "description": "summary",
        "created_at": "createdAt",
        "updated_at": "updatedAt",
        "version": "externalVersion",
        "engine": "engine",
        "protocol": "protocol",
        "model_type": "modelType",
        "data_format": "dataFormat",
        "type": "type",
        "embedding_model_id": "embeddingModelId",
    }
    for field, target in fields.items():
        value = row.get(field)
        if isinstance(value, str):
            result[target] = value[:30000]
        elif field == "type" and type(value) is int:
            result[target] = value
    for field, target in (
        ("record_count", "recordCount"),
        ("file_size", "fileSize"),
        ("status", "externalStatus"),
        ("valid_code", "validCode"),
    ):
        if type(row.get(field)) is int and row[field] >= 0:
            result[target] = row[field]
    for field, target in (("is_publish", "isPublished"), ("is_public", "isPublic")):
        if isinstance(row.get(field), bool):
            result[target] = row[field]
    if module == "knowledge":
        kind = (row.get("data") or {}).get("type") if isinstance(row.get("data"), dict) else None
        if isinstance(kind, str):
            result["knowledgeType"] = kind[:100]
        result["documentsAvailable"] = str(kind).lower() not in ("pageindex", "page_index", "index")
    result.setdefault("summary", "")
    result["snapshot"] = fingerprint(result)
    return result


class HuaweiCatalog:
    """读取完整分页目录与单个智能体详情，拒绝把不完整分页当作删除依据。"""

    def __init__(self, client: HuaweiClient) -> None:
        self.client = client

    def rows(self, module: str, renew: Callable[[], None] | None = None) -> list[dict]:
        """全量查询最多一万条记录；分页总数变化时保留上次成功快照。"""
        items, seen, expected, total_size = [], set(), None, 0
        for page in range(1, 101):
            if renew:
                renew()
            data = object_result(
                self.client.request(CATALOG_PATHS[module], {"page": page, "size": 100})
            )
            total = data.get("total")
            if type(total) is not int or not 0 <= total <= 10000:
                raise HTTPException(502, "华为工具链分页总数无效或超过一万条")
            if expected is not None and expected != total:
                raise HTTPException(409, "华为资源目录在查询期间发生变化，请稍后刷新")
            expected = total
            current = list_result(data.get("data"))
            if len(current) > 100:
                raise HTTPException(502, "华为工具链返回的记录数超过请求页大小")
            for row in current:
                item = metadata(module, row)
                identifier = item["externalId"]
                if identifier in seen:
                    raise HTTPException(502, "华为工具链分页返回重复资源")
                seen.add(identifier)
                total_size += len(json.dumps(item, ensure_ascii=False).encode())
                if total_size > 8_000_000:
                    raise HTTPException(502, "华为资源目录过大")
                items.append(item)
            if len(items) == total:
                return items
            if not current or len(items) > total:
                raise HTTPException(502, "华为工具链分页不完整")
        raise HTTPException(502, "华为工具链分页超过处理上限")

    def fetch(self, module: str, renew: Callable[[], None]) -> list[dict]:
        """生成有大小上限的脱敏目录快照。"""
        return self.rows(module, renew)

    def detail(self, module: str, identifier: str) -> dict:
        """仅智能体有按 ID 详情；其他模块通过列表重新定位，不编造详情接口。"""
        if module == "applications":
            row = object_result(self.client.request(AGENT_DETAIL, {"id": identifier}))
            result = metadata(module, row)
        else:
            result = next(
                (row for row in self.rows(module) if row["externalId"] == identifier), None
            )
            if result is None:
                raise HTTPException(404, "华为工具链中不存在该资源")
        if result["externalId"] != identifier:
            raise HTTPException(502, "华为工具链详情编号不匹配")
        return result

    def documents(self, identifier: str, page: int, size: int) -> dict:
        """读取普通知识库文档元数据，外部文件编号不作为平台 files 外键。"""
        data = object_result(
            self.client.request(
                DOCUMENT_LIST,
                {"knowledge_id": identifier, "page": page, "size": size},
            )
        )
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise HTTPException(502, "华为文档分页总数无效")
        items = []
        statuses = {
            0: "待处理",
            1: "解析中",
            2: "分片中",
            3: "生成中",
            4: "可用",
            5: "失败",
            6: "已停止",
            7: "停止中",
        }
        rows = list_result(data.get("data"))
        if len(rows) > size or len(rows) > total:
            raise HTTPException(502, "华为文档分页记录数无效")
        for row in rows:
            if row.get("knowledge_id") != identifier:
                raise HTTPException(502, "华为返回了其他知识库的文档")
            item = metadata("documents", row)
            status = row.get("status")
            item["statusLabel"] = statuses.get(status, "未知") if type(status) is int else "未知"
            for field, target in (("segment_count", "segmentCount"), ("file_size", "fileSize")):
                value = row.get(field)
                if type(value) is int and value >= 0:
                    item[target] = value
            items.append(item)
        return {"items": items, "total": total, "page": page, "pageSize": size}
