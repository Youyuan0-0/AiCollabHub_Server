"""开发用外部资料快照；模拟目录不写入平台业务表，不调用供应方服务。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from fastapi import HTTPException

LOCAL_FIELDS = frozenset(
    (
        "englishName",
        "contact",
        "tutorials",
        "usageLimits",
        "technicalStack",
        "deployment",
        "triggerType",
        "dependencies",
        "configOptions",
    )
)
METRICS = frozenset(
    (
        "usage",
        "rating",
        "ratingCount",
        "downloadCount",
        "favoriteCount",
        "lastDownloadedAt",
        "liveUsage",
        "simulationUsage",
        "metricsScope",
    )
)


class DemoMarketplaceStore:
    """按内容生成不可变快照；只有数据库已提交的引用才对业务可见。

    SQL 回滚时最多留下未引用的模拟文件，不会提前发布应用或覆盖旧草稿。
    同一开发目录可供本机多个进程读取；实际多服务器应使用供应方服务。
    """

    profile = "demo-file"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()

    def save(self, kind: str, data: dict) -> str:
        """以内容摘要保存不可变模拟快照，原子替换文件并返回引用。"""
        if kind not in ("listing", "submission", "decision", "download", "development"):
            raise ValueError("Unsupported demo snapshot kind")
        content = json.dumps(
            {"kind": kind, "data": data},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        reference = f"{kind}-{sha256(content).hexdigest()}"
        folder = self.directory / kind
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{reference}.json"
            if target.exists():
                if target.read_bytes() != content:
                    raise OSError("Demo snapshot content changed")
                return reference
            descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=folder)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            return reference
        except OSError:
            raise HTTPException(503, "application_demo_storage_unavailable") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def load(self, reference: str, kind: str) -> dict:
        """读取指定类型的快照并校验引用格式、摘要与内容结构。"""
        if not re.fullmatch(re.escape(kind) + r"-[0-9a-f]{64}", reference or ""):
            raise HTTPException(503, "application_demo_reference_invalid")
        try:
            content = (self.directory / kind / f"{reference}.json").read_bytes()
            if sha256(content).hexdigest() != reference.removeprefix(kind + "-"):
                raise ValueError("Snapshot checksum mismatch")
            record = json.loads(content)
            if record["kind"] != kind or not isinstance(record["data"], dict):
                raise ValueError("Snapshot type mismatch")
            return record["data"]
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "application_demo_snapshot_unavailable") from None

    def developments(self) -> list[tuple[str, dict]]:
        """按文件名返回开发快照引用及内容，目录不存在时返回空列表。"""
        folder = self.directory / "development"
        if not folder.exists():
            return []
        return [
            (item.stem, self.load(item.stem, "development"))
            for item in sorted(folder.glob("development-*.json"))
        ]

    def save_submission(self, record: dict) -> str:
        """剔除本地管理字段后保存模拟外部提交快照。"""
        external = {
            key: deepcopy(value)
            for key, value in record.items()
            if key
            not in (
                "reviews",
                "securityLevel",
                "priority",
                "createdBy",
                "createdByName",
                "organizationId",
                "submissionRules",
                "formRevision",
            )
        }
        external["data"] = {
            key: deepcopy(value) for key, value in record["data"].items() if key not in LOCAL_FIELDS
        }
        return self.save("submission", external)

    def save_listing(self, resource: dict) -> str:
        """剔除本地权限与运行指标后保存模拟外部条目快照。"""
        return self.save(
            "listing",
            {
                key: deepcopy(value)
                for key, value in resource.items()
                if key
                not in LOCAL_FIELDS
                | METRICS
                | {
                    "createdBy",
                    "createdByName",
                    "organizationId",
                    "securityLevel",
                }
            },
        )
