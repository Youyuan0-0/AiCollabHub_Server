"""华为智能体元数据导入；复用平台应用申请，不把配置版本视为可运行版本。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from app.modules.applications.publication_schemas import (
    ApplicationDetails,
    SaveApplicationPublication,
)
from app.modules.common.identity import Actor, can_manage
from app.modules.common.toolchain.catalog import ToolchainCatalogs
from app.modules.common.toolchain.contracts import authorize, fingerprint
from app.modules.common.toolchain.huawei import HuaweiCatalog

if TYPE_CHECKING:
    from app.modules.applications.application_service import ApplicationService


class HuaweiAgentCatalogProvider:
    """仅提供目录和草稿元数据，运行等能力由原提供器管理。"""

    provider_name = "huawei-platform"

    def __init__(self, delegate: object, catalogs: ToolchainCatalogs) -> None:
        self.delegate = delegate
        self.catalogs = catalogs
        self.client = catalogs.clients["applications"]
        self.toolchain_mode = self.client.mode

    def __getattr__(self, name: str) -> object:
        """复用平台已有业务能力。"""
        return getattr(self.delegate, name)

    def authorize(self, actor: Actor) -> None:
        """校验平台发布者及外部账号访问范围。"""
        authorize(self.client.settings, actor)

    def reference(self, identifier: str) -> dict:
        """返回包含外部账号范围的来源。"""
        return self.catalogs.reference("applications", identifier)

    def toolchain_resources(self) -> list[dict]:
        """展示目录快照；同步令牌独立于供应方配置版本。"""
        return [
            {
                **item,
                "workspace": "",
                "category": "",
                "versions": [item["snapshot"]],
                "versionDetails": [{"code": item["snapshot"], "name": "当前资料快照"}],
            }
            for item in self.catalogs.state("applications")["items"]
        ]

    def toolchain_resource(self, external_id: str, version: str) -> dict:
        """导入前重新查询详情并核对快照，不保存 config 或 engine_config。"""
        item = HuaweiCatalog(self.client).detail("applications", external_id)
        if item["snapshot"] != version:
            raise HTTPException(409, "华为智能体资料已变化，请刷新详情后导入")
        data = ApplicationDetails(
            name=item["name"],
            summary=item["summary"][:1000],
            description_markdown=item["summary"],
        ).model_dump(by_alias=True)
        return {
            "data": data,
            "workspace": "",
            "snapshot": item["snapshot"],
            "externalVersion": item.get("externalVersion"),
            "agentStatus": item.get("externalStatus"),
        }

    def develop(self, data: dict, actor_id: str) -> dict:
        """当前华为文档仅提供查询接口。"""
        raise HTTPException(422, "当前华为接入未提供智能体开发接口")

    def import_draft(
        self, service: ApplicationService, actor: Actor, identifier: str, snapshot: str
    ) -> dict:
        """在发布事务内按持久来源去重；不依赖 Redis 短期同步绑定。"""
        self.authorize(actor)
        result = self.toolchain_resource(identifier, snapshot)
        reference = self.reference(identifier)
        with service.repository.transaction() as session:
            if hasattr(session, "lock"):
                session.lock("toolchain:" + fingerprint(reference))
            records = [
                item
                for item in session.list_publications()
                if all(
                    item.get("source", {}).get(field) == value for field, value in reference.items()
                )
            ]
            if any(
                item["createdBy"] != actor.id and not can_manage(actor, item) for item in records
            ):
                raise HTTPException(403, "该工具链资源已由其他发布者登记")
            existing = next(
                (item for item in records if item["source"].get("snapshot") == snapshot), None
            )
            if existing:
                return {"status": "unchanged", "publicationId": existing["id"]}
            if any(item["status"] != "approved" for item in records):
                raise HTTPException(409, "该应用有未完成的申请，请先处理后再同步新资料")
            base_resource = session.get_resource(records[0]["applicationId"]) if records else None
            if records and base_resource is None:
                raise HTTPException(409, "来源应用状态异常，请管理员检查")
            source = {
                "kind": "toolchain",
                **reference,
                "snapshot": snapshot,
                "externalVersion": result["externalVersion"],
                "externalStatus": result["agentStatus"],
                "syncedAt": datetime.now(UTC).isoformat(),
            }
            record = service._save(
                session,
                SaveApplicationPublication(data=ApplicationDetails.model_validate(result["data"])),
                actor=actor,
                base_resource=base_resource,
                source=source,
            )
            return {"status": "succeeded", "publicationId": record["id"]}
