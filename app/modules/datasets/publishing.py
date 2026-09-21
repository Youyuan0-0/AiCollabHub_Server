"""数据集发布适配层：本地上架先行，第三方接入保留独立通道和稳定业务编号。"""

import csv
import io
import json
from typing import Literal, Protocol

from app.modules.datasets.schemas import DatasetInput, validate_submission

PublicationMode = Literal["local", "external"]


def sample_preview(sample: str) -> dict:
    """把已审核的公开样例转为表格；无法解析时保留原文，空样例不生成下载内容。"""
    if not sample.strip():
        return {"columns": [], "rows": []}
    try:
        value = json.loads(sample)
    except ValueError:
        value = None
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        columns = list(dict.fromkeys(key for row in value for key in row))
        return {
            "columns": columns,
            "rows": [
                [
                    json.dumps(row.get(key), ensure_ascii=False)
                    if not isinstance(row.get(key), str)
                    else row[key]
                    for key in columns
                ]
                for row in value
            ],
        }
    rows = list(
        csv.reader(io.StringIO(sample), delimiter="\t" if "\t" in sample.splitlines()[0] else ",")
    )
    if len(rows) > 1 and rows[0] and all(len(row) == len(rows[0]) for row in rows):
        return {"columns": rows[0], "rows": rows[1:]}
    return {"columns": ["公开样例"], "rows": [[sample]]}


class DatasetPublisher(Protocol):
    """返回发布状态和资源快照，由调用方与审核结果一起按修订号提交。"""

    def prepare(self, record: dict) -> dict:
        """record 为已审核申请；实现不得在数据库提交前执行不可回滚的外部发布。"""
        ...


class LocalDatasetPublisher:
    """将已审核元数据及公开样例直接上架到平台，不伪造第三方资源编号。"""

    def prepare(self, record: dict) -> dict:
        """校验后构造稳定 datasetId 的目录快照；联系人和密级仍受公开字段白名单保护。"""
        data = DatasetInput(**record["data"])
        validate_submission(data)
        resource = {
            **data.model_dump(by_alias=True),
            "id": record["datasetId"],
            "kind": "dataset",
            "description": data.summary,
            "organizationId": record["organizationId"],
            "createdBy": record["createdBy"],
            "category": data.data_application[0],
            "usage": 0,
            "rating": None,
            "qualityScore": None,
            "status": "available",
            "tone": "blue",
            "updatedAt": record["updatedAt"],
            "publishedAt": record["updatedAt"],
            "publicationSource": "local",
            "source": record.get("source"),
            "preview": sample_preview(data.sample_data),
            "facets": {
                "dataApplication": data.data_application,
                "dataShape": data.data_shape,
                "aiPurpose": [data.ai_purpose],
                "language": data.language,
                "dataFormat": data.data_format,
            },
            "versions": [
                {
                    "version": data.version,
                    "releasedAt": record["updatedAt"],
                    "status": "current",
                    "note": data.version_notes,
                }
            ],
        }
        return {
            "syncStatus": "local_published",
            "publicationProvider": "local",
            "publishedAt": record["updatedAt"],
            "publishedResource": resource,
        }


class ExternalDatasetPublisher:
    """第三方协议未提供时只保留待同步状态，不影响审核结论，也不伪造上架成功。"""

    def prepare(self, record: dict) -> dict:
        """后续任务可用申请编号和修订号作幂等键，外部确认成功后再保存目录快照。"""
        return {"syncStatus": "pending", "publicationProvider": "external"}


def create_dataset_publisher(mode: PublicationMode) -> DatasetPublisher:
    """按配置选择发布通道，已保存的目录快照不随通道切换被删除。"""
    return LocalDatasetPublisher() if mode == "local" else ExternalDatasetPublisher()
