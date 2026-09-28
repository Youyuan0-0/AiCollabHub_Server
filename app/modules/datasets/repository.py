"""数据集仓储；正式模式使用独立资源表和共用明细，内存实现仅供隔离测试。"""

import json
from copy import deepcopy
from hashlib import sha256
from threading import RLock

from fastapi import HTTPException
from sqlalchemy import Engine, select, update

from app.core.config import Settings
from app.db.foundation import FoundationDatabase, identifier, utc_now
from app.modules.common.resource_database import ResourceDatabase
from app.modules.common.resource_runtime import ResourceRuntime


class DatasetRepository:
    """保持数据集服务的记录契约，将各类业务明确映射到对应的新表。"""

    def __init__(
        self,
        engine: Engine | None = None,
        settings: Settings | None = None,
        *,
        foundation: FoundationDatabase | None = None,
        runtime: ResourceRuntime | None = None,
    ) -> None:
        self.engine = engine
        self.database = (
            ResourceDatabase(
                foundation or FoundationDatabase(engine, settings or Settings(_env_file=None)),
                "dataset",
                runtime=runtime,
            )
            if foundation is not None or engine is not None
            else None
        )
        self._records: dict[str, dict] = {}
        self._lock = RLock()

    def resources(self) -> list[dict]:
        """读取正式目录，数据库模式不会混入静态演示卡片。"""
        if self.database is None:
            return []
        with self.database.transaction(is_readonly=True) as session:
            return session.list_resources()

    def save_metadata(self, dataset_id: str, values: dict, expected: int) -> dict:
        """按资源修订保存交付元数据；数据库与内存模式均拒绝过期写入。"""
        if self.database is None:
            with self._lock:
                key = "metadata:" + dataset_id
                current = self._records.get(key)
                current_revision = current["revision"] if current else expected
                if current_revision != expected:
                    raise HTTPException(409, "数据集已更新，请刷新后重试")
                result = {**deepcopy(values), "revision": expected + 1}
                self._records[key] = result
                return deepcopy(result)
        with self.database.transaction() as session:
            resource = session.get_resource(dataset_id)
            if resource is None or resource["revision"] != expected:
                raise HTTPException(409, "数据集已更新，请刷新后重试")
            table = session.table("datasets")
            session.db.execute(
                update(table)
                .where(table.c.id == identifier(dataset_id))
                .values(
                    data_lineage=values["dataLineage"] or None,
                    storage_location=values["storageLocation"] or None,
                    api_endpoint=values["apiEndpoint"] or None,
                    updated_at=utc_now(),
                )
            )
            row = session.db.execute(
                select(table).where(table.c.id == identifier(dataset_id))
            ).mappings().one()
            return {
                "dataLineage": row["data_lineage"] or "",
                "storageLocation": row["storage_location"] or "",
                "apiEndpoint": row["api_endpoint"] or "",
                "updatedAt": row["updated_at"].isoformat(),
                "revision": int(row["updated_at"].timestamp() * 1_000_000),
            }

    @staticmethod
    def activity_view(value: dict) -> dict:
        """兼容数据集当前互动响应；修订指纹用于比较内容，不占用额外表字段。"""
        result = {
            **value,
            "userId": value["actorId"],
            "downloadCount": value.get("downloadCount", 0),
        }
        result["ratingUpdatedAt"] = value.get("updatedAt")
        result["revision"] = int(
            sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12], 16
        )
        return result

    def list(self, prefix: str) -> list[dict]:
        """按已知业务种类读取，不在数据库保存可随意扩展的键值记录。"""
        if self.database is None:
            with self._lock:
                return deepcopy(
                    [value for key, value in self._records.items() if key.startswith(prefix)]
                )
        with self.database.transaction(is_readonly=True) as session:
            if prefix == "publication:":
                return session.list_publications()
            if prefix == "activity:":
                return [self.activity_view(item) for item in session.entries("interactions")]
            if prefix in {"listing:", "visibility:"}:
                return [
                    {
                        "datasetId": item["id"],
                        **(
                            {"status": item["listingStatus"], "revision": item["revision"]}
                            if prefix == "listing:"
                            else session.get_visibility(item["id"])
                        ),
                    }
                    for item in session.list_resources()
                ]
            return session.entries(prefix.rstrip(":"))

    def get(self, key: str) -> dict | None:
        """按业务编号读取，用户身份始终来自已验证的服务参数。"""
        if self.database is None:
            with self._lock:
                return deepcopy(self._records.get(key))
        kind, _, item_id = key.partition(":")
        with self.database.transaction(is_readonly=True) as session:
            if kind == "publication":
                return session.get_publication(item_id)
            if kind == "listing":
                resource = session.get_resource(item_id)
                return (
                    {
                        "datasetId": item_id,
                        "status": resource["listingStatus"],
                        "revision": resource["revision"],
                    }
                    if resource
                    else None
                )
            if kind == "visibility":
                return {"datasetId": item_id, **session.get_visibility(item_id)}
            value = session.entry("interactions" if kind == "activity" else kind, item_id)
            return self.activity_view(value) if kind == "activity" and value else value

    def save(self, key: str, data: dict, expected: int | None) -> dict:
        """比较修订并原子保存；审核记录和正式版本由共用发布事务保证一致。"""
        result = {**deepcopy(data), "revision": (expected or 0) + 1}
        conflict = HTTPException(409, "记录已更新，请刷新后重试；当前输入尚未覆盖服务端内容")
        if self.database is None:
            with self._lock:
                current = self._records.get(key)
                if (current["revision"] if current else None) != expected:
                    raise conflict
                self._records[key] = deepcopy(result)
            return result
        kind, _, item_id = key.partition(":")
        with self.database.transaction() as session:
            if kind == "publication":
                if result.get("publishedResource"):
                    session.put_resource(result["publishedResource"])
                session.put_publication(result)
            elif kind == "listing":
                resource = session.get_resource(item_id)
                if resource is None or resource["revision"] != expected:
                    raise conflict
                resource["listingStatus"] = result["status"]
                session.save_resource_state(resource)
                result["revision"] = session.get_resource(item_id)["revision"]
            elif kind == "visibility":
                current = session.get_visibility(item_id)
                if current["revision"] != (expected or 0):
                    raise conflict
                session.put_visibility(item_id, result)
            elif kind == "activity":
                current = session.entry("interactions", item_id)
                if (self.activity_view(current)["revision"] if current else None) != expected:
                    raise conflict
                session.put_entry("interactions", item_id, {**result, "actorId": result["userId"]})
            else:
                current = session.entry(kind, item_id)
                if (current.get("revision") if current else None) != expected:
                    raise conflict
                session.put_entry(kind, item_id, result)
        return result
