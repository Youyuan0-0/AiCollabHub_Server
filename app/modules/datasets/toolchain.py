"""Data Engine 成果读取和草稿导入，依据知识引擎指南 P22–94。"""

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.modules.common.identity import Actor
from app.modules.common.service import timestamp
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.contracts import (
    authorize,
    fingerprint,
    list_result,
    object_result,
    segment,
    source_reference,
)
from app.modules.datasets.schemas import DatasetInput
from app.modules.datasets.workspace import DatasetWorkspace

BASE = "/gatectl/data-engine/api/v2"


class DatasetToolchain:
    """通过共用 POP 客户端读取成果，复用数据集仓储及审核资料格式。"""

    def __init__(self, client: PopClient, workspace: DatasetWorkspace) -> None:
        self.client = client
        self.workspace = workspace

    def catalog(self, actor: Actor, page: int, size: int, keyword: str) -> dict:
        """查询当前授权工作空间的分页成果，不将供应方所有者当作平台单位。"""
        authorize(self.client.settings, actor)
        data = object_result(
            self.client.request(
                "GET",
                BASE + "/datasets",
                action="GetDataEngineApiV2Datasets",
                query={"pageNumber": page, "pageSize": size, "keyword": keyword},
            )
        )
        items = list_result(data.get("datasets"))
        return {
            "items": [
                {
                    "externalId": item["code"],
                    "name": item.get("name", ""),
                    "summary": item.get("description", ""),
                    "updatedAt": item.get("updatedAt"),
                    "recordCount": item.get("rowCount"),
                }
                for item in items
                if isinstance(item.get("code"), str)
            ],
            "total": data.get("totalCount", len(items)),
            "page": page,
            "pageSize": size,
            "mode": self.client.mode,
        }

    def detail(self, actor: Actor, identifier: str) -> dict:
        """读取资料并转换为可编辑草稿；缺失字段保持空值，禁止伪造版本和评分。"""
        authorize(self.client.settings, actor)
        raw = object_result(
            self.client.request(
                "GET",
                BASE + "/datasets/" + segment(identifier),
                action="GetDataEngineApiV2DatasetsCode",
            )
        )
        if raw.get("code") != identifier:
            raise HTTPException(502, "工具链返回的数据集编号不匹配")
        properties: dict = {}
        type_mapping = {
            "string": "string",
            "int": "integer",
            "integer": "integer",
            "long": "integer",
            "float": "number",
            "double": "number",
            "number": "number",
            "boolean": "boolean",
            "bool": "boolean",
        }
        for subset in list_result(raw.get("subsets", [])):
            for field in list_result(subset.get("features", [])):
                name = field.get("columnName")
                if not isinstance(name, str) or not name:
                    continue
                schema = {}
                if kind := type_mapping.get(str(field.get("columnType", "")).lower()):
                    schema["type"] = kind
                if field.get("columnComment"):
                    schema["description"] = field["columnComment"]
                if name in properties and properties[name] != schema:
                    raise HTTPException(422, "不同子集的字段定义不一致，请分别整理后发布")
                properties[name] = schema
        tags = [item.get("value") or item.get("name") for item in list_result(raw.get("tags", []))]
        data = DatasetInput(
            name=raw.get("name", ""),
            summary=raw.get("description", "")[:1000],
            description_markdown=raw.get("description", ""),
            tags=[tag for tag in tags if isinstance(tag, str) and tag.strip()][:20],
            field_schema=json.dumps(
                {"type": "object", "properties": properties}, ensure_ascii=False
            )
            if properties
            else "",
            record_count=raw.get("rowCount"),
            data_size=f"{raw['originalFileSize']} bytes"
            if raw.get("originalFileSize") is not None
            else "",
        ).model_dump(by_alias=True)
        # 这是外部元数据快照指纹，不伪装成供应方版本号；平台版本仍由平台维护。
        source = {
            **source_reference(self.client.settings, "data-engine", identifier),
            "snapshot": fingerprint(
                {key: value for key, value in raw.items() if key != "downloadCount"}
            ),
            "externalVersion": None,
        }
        return {
            "data": data,
            "source": source,
            "mode": self.client.mode,
            "missingFields": ["所属单位", "联系人", "分类", "语言", "格式", "数据分级"],
            "subsets": [
                {"code": item.get("code"), "splits": item.get("splits", [])}
                for item in list_result(raw.get("subsets", []))
            ],
        }

    def preview(self, actor: Actor, identifier: str, subset: str, split: str) -> dict:
        """预览只在构建成功后返回，导入时不自动把未审核样例公开。"""
        authorize(self.client.settings, actor)
        data = object_result(
            self.client.request(
                "GET",
                BASE + "/datasets/" + segment(identifier) + "/viewer",
                action="GetDataEngineApiV2DatasetsCodeViewer",
                query={"subset": subset, "split": split, "pageNumber": 1, "pageSize": 10},
            )
        )
        status = data.get("status")
        if status not in ("BUILDING", "BUILT", "BUILD_FAILED"):
            raise HTTPException(502, "工具链返回了未知预览状态")
        return {
            "status": status,
            "schema": data.get("schema", []) if status == "BUILT" else [],
            "rows": data.get("data", []) if status == "BUILT" else [],
            "mode": self.client.mode,
        }

    def task(self, actor: Actor, task_id: str, run_id: str) -> dict:
        """保留任务与运行的不同编号，供加工完成后定位输出数据集。"""
        authorize(self.client.settings, actor)
        path = BASE + "/process/tasks/" + segment(task_id)
        task = self.client.request("GET", path, action="GetDataEngineApiV2ProcessTasksTaskid")
        instances = self.client.request(
            "GET",
            path + "/instances",
            action="GetDataEngineApiV2ProcessTasksTaskidInstances",
            query={"runId": run_id},
        )
        return {"task": task, "instances": instances, "mode": self.client.mode}

    def files(self, actor: Actor, identifier: str, page: int, size: int) -> dict:
        """读取成果文件元数据，完整文件下载授权仍由所属工具链管理。"""
        authorize(self.client.settings, actor)
        data = object_result(
            self.client.request(
                "GET",
                BASE + "/datasets/" + segment(identifier) + "/files",
                action="GetDataEngineApiV2DatasetsCodeFiles",
                query={"pageNumber": page, "pageSize": size, "relative": True},
            )
        )
        return {
            "items": list_result(data.get("datasetFileInfos")),
            "total": data.get("totalCount"),
            "mode": self.client.mode,
        }

    def download(self, actor: Actor, identifier: str, file_path: str, page: int) -> dict:
        """复查当前页文件归属后按次获取临时链接，不保存链接或接受任意路径。"""
        files = self.files(actor, identifier, page, 20)
        if not any(item.get("filepath") == file_path for item in files["items"]):
            raise HTTPException(404, "文件不在当前成果页中，请刷新文件列表")
        if self.client.mode == "mock":
            raise HTTPException(422, "模拟成果不提供真实文件下载")
        data = object_result(
            self.client.request(
                "GET",
                BASE + "/files/presigned/url",
                action="GetDataEngineApiV2FilesPresignedUrl",
                query={"datasetCode": identifier, "filePath": file_path, "useShortUrl": False},
            )
        )
        try:
            url = data.get("url")
            if not isinstance(url, str) or any(ord(char) < 32 for char in url):
                raise ValueError("invalid url")
            parts = urlsplit(url)
            expiration = data.get("expiration")
            expires = datetime.fromisoformat(data["expireTime"].replace("Z", "+00:00"))
            if (
                parts.scheme not in ("https", "http")
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.fragment
                or not isinstance(expiration, int)
                or isinstance(expiration, bool)
                or expiration <= 0
                or expires.tzinfo is None
                or expires <= datetime.now(UTC)
            ):
                raise ValueError("invalid download link")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(502, "工具链未返回有效且未过期的下载地址") from None
        return {"url": url, "expiration": expiration, "expireTime": data["expireTime"]}

    def import_draft(self, actor: Actor, identifier: str, snapshot: str, identity: dict) -> dict:
        """按来源快照幂等导入，来源占用通过仓储修订号协调，不覆盖未完成申请。"""
        result = self.detail(actor, identifier)
        source = result["source"]
        if source["snapshot"] != snapshot:
            raise HTTPException(409, "工具链成果已更新，请刷新详情后重新导入")
        repository = self.workspace.repository
        source_key = fingerprint(source_reference(self.client.settings, "data-engine", identifier))
        binding_key = "toolchain:" + source_key
        publication_id = "dsp-tc-" + fingerprint([source_key, snapshot])[:40]
        publication_key = "publication:" + publication_id
        previous = repository.get(binding_key)
        if previous and previous["createdBy"] != actor.id:
            raise HTTPException(403, "该工具链资源已由其他发布者登记")
        existing = repository.get(publication_key)
        if existing:
            if existing["createdBy"] != actor.id:
                raise HTTPException(403, "该工具链资源已由其他发布者登记")
            return {"status": "unchanged", "publicationId": existing["id"], "source": source}
        if previous:
            old = repository.get("publication:" + previous["publicationId"])
            if previous["snapshot"] != snapshot and (old is None or old["status"] != "approved"):
                raise HTTPException(409, "此数据集还有未完成的导入或申请，请先处理")
        # 先保留确定的申请编号；中断后同一快照可恢复，不重复生成申请。
        repository.save(
            binding_key,
            {
                "createdBy": actor.id,
                "publicationId": publication_id,
                "snapshot": snapshot,
            },
            previous["revision"] if previous else None,
        )
        now = timestamp()
        data = {
            **result["data"],
            "owner": identity.get("owner", ""),
            "contact": identity.get("contact", ""),
        }
        count = sum(
            1
            for record in repository.list("publication:")
            if record.get("datasetId") == "ds-tc-" + source_key[:40]
        )
        data["version"] = f"v{count + 1}.0"
        record = {
            "id": publication_id,
            "datasetId": "ds-tc-" + source_key[:40],
            "createdBy": actor.id,
            "organizationId": actor.organization_id,
            "data": data,
            "status": "draft",
            "createdAt": now,
            "updatedAt": now,
            "submittedAt": None,
            "reviewComment": "",
            "reviews": [],
            "syncStatus": "imported",
            "source": {**source, "syncedAt": now},
        }
        try:
            repository.save(publication_key, record, None)
        except HTTPException as error:
            if error.status_code != 409 or repository.get(publication_key) is None:
                raise
        return {"status": "succeeded", "publicationId": publication_id, "source": source}
