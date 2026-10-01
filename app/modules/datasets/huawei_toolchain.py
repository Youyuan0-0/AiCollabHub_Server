"""华为数据集元数据导入，平台版本和外部集合版本独立维护。"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from app.modules.common.identity import Actor
from app.modules.common.toolchain.catalog import ToolchainCatalogs
from app.modules.common.toolchain.contracts import authorize, fingerprint
from app.modules.common.toolchain.huawei import HuaweiCatalog
from app.modules.datasets.schemas import DatasetInput
from app.modules.datasets.toolchain import DatasetToolchain
from app.modules.datasets.workspace import DatasetWorkspace


class HuaweiDatasetToolchain(DatasetToolchain):
    """复用幂等草稿导入，只替换来源、查询协议及本地标识生成。"""

    def __init__(self, catalogs: ToolchainCatalogs, workspace: DatasetWorkspace) -> None:
        self.catalogs = catalogs
        self.client = catalogs.clients["datasets"]
        self.workspace = workspace

    def reference(self, identifier: str) -> dict:
        """供应方编号及 API Key 范围作为外部来源，不充当本地主键。"""
        return self.catalogs.reference("datasets", identifier)

    def import_identifiers(self, source_key: str, snapshot: str) -> tuple[str, str]:
        """生成可被 PostgreSQL UUID 列接受的稳定平台资源和申请编号。"""
        return (
            str(uuid5(NAMESPACE_URL, "huawei:dataset:" + source_key)),
            str(uuid5(NAMESPACE_URL, "huawei:dataset-publication:" + source_key + ":" + snapshot)),
        )

    def catalog(self, actor: Actor, page: int, size: int, keyword: str) -> dict:
        """读取已经完成的目录快照。"""
        authorize(self.client.settings, actor)
        return self.catalogs.page("datasets", page, size, keyword)

    def import_draft(self, actor: Actor, identifier: str, snapshot: str, identity: dict) -> dict:
        """按持久申请来源去重，并在同一事务中保护账号归属和新版本检查。"""
        result = self.detail(actor, identifier)
        if result["source"]["snapshot"] != snapshot:
            raise HTTPException(409, "华为数据集资料已变化，请刷新详情后导入")
        reference = self.reference(identifier)
        source_key = fingerprint(reference)
        dataset_id, publication_id = self.import_identifiers(source_key, snapshot)
        repository = self.workspace.repository
        # 数据库事务或开发内存锁覆盖来源检查与保存，避免并发导入不同快照。
        with (
            repository.database.transaction() if repository.database else repository._lock
        ) as session:
            if repository.database:
                session.lock("toolchain:" + source_key)
            publications = (
                session.list_publications()
                if repository.database
                else repository.list("publication:")
            )
            records = [
                item
                for item in publications
                if all(
                    item.get("source", {}).get(field) == value for field, value in reference.items()
                )
            ]
            if any(item["createdBy"] != actor.id for item in records):
                raise HTTPException(403, "该工具链资源已由其他发布者登记")
            existing = next(
                (item for item in records if item["source"].get("snapshot") == snapshot), None
            )
            if existing:
                return {
                    "status": "unchanged",
                    "publicationId": existing["id"],
                    "source": result["source"],
                }
            if any(item["status"] != "approved" for item in records):
                raise HTTPException(409, "此数据集还有未完成的申请，请先处理")
            now = datetime.now(UTC).isoformat()
            data = {
                **result["data"],
                "owner": identity.get("owner", ""),
                "contact": identity.get("contact", ""),
                "version": f"v{len(records) + 1}.0",
            }
            record = {
                "id": publication_id,
                "datasetId": dataset_id,
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
                "revision": 1,
                "source": {**result["source"], "syncedAt": now},
            }
            if repository.database:
                session.put_publication(record)
            else:
                repository.save("publication:" + publication_id, record, None)
            return {
                "status": "succeeded",
                "publicationId": publication_id,
                "source": result["source"],
            }

    def detail(self, actor: Actor, identifier: str) -> dict:
        """重新从集合列表定位资源；当前文档没有数据集详情接口。"""
        authorize(self.client.settings, actor)
        item = HuaweiCatalog(self.client).detail("datasets", identifier)
        data = DatasetInput(
            name=item["name"],
            summary=item["summary"][:1000],
            description_markdown=item["summary"],
            record_count=item.get("recordCount"),
            data_format=[item["dataFormat"]] if item.get("dataFormat") else [],
            data_size=f"{item['fileSize']} bytes" if "fileSize" in item else "",
        ).model_dump(by_alias=True)
        return {
            "data": data,
            "mode": self.client.mode,
            "subsets": [],
            "source": {
                **self.reference(identifier),
                "snapshot": item["snapshot"],
                "externalVersion": item.get("externalVersion"),
            },
            "missingFields": [
                "所属单位",
                "联系人",
                "字段清单",
                "数据应用",
                "数据形态",
                "语言",
                "数据分级",
            ],
        }

    def preview(self, actor: Actor, identifier: str, subset: str, split: str) -> dict:
        """预览接口未在当前文档交付。"""
        raise HTTPException(422, "华为接入未提供数据集预览接口")

    def files(self, actor: Actor, identifier: str, page: int, size: int) -> dict:
        """文件编号不能解释成文件列表或下载链接。"""
        raise HTTPException(422, "华为接入未提供数据集文件列表接口")

    def download(self, actor: Actor, identifier: str, file_path: str, page: int) -> dict:
        """下载接口未在当前文档交付。"""
        raise HTTPException(422, "华为接入未提供数据集下载接口")

    def task(self, actor: Actor, task_id: str, run_id: str) -> dict:
        """加工任务接口未在当前文档交付。"""
        raise HTTPException(422, "华为接入未提供加工任务接口")
