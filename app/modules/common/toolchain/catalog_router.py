"""四个模块的外部目录入口，所有查询与手动刷新均由后端核验身份。"""

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field

from app.modules.common.access_schemas import StrictPayload
from app.modules.common.identity import current_actor
from app.modules.common.identity_directory import publication_defaults
from app.modules.common.toolchain.catalog import ToolchainCatalogs
from app.modules.common.toolchain.configuration import module_entry_url, module_settings
from app.modules.common.toolchain.contracts import authorize
from app.modules.common.toolchain.huawei import HuaweiCatalog
from app.modules.datasets.huawei_toolchain import HuaweiDatasetToolchain

Module = Literal["applications", "datasets", "knowledge", "models"]
router = APIRouter(prefix="/external-toolchain", tags=["external toolchain"])


class CatalogImport(StrictPayload):
    """提交详情对应的来源快照；平台归属和审核状态不由客户端传入。"""

    snapshot: str = Field(pattern=r"^[a-f0-9]{64}$")


def catalogs(request: Request, module: str) -> ToolchainCatalogs:
    """先检查模块及授权，再访问账号隔离的外部快照。"""
    settings = module_settings(request.app.state.settings, module, "huawei")
    authorize(settings, current_actor(request))
    service = request.app.state.toolchain_catalogs
    if module not in service.clients:
        raise HTTPException(422, "当前模块未使用华为查询连接")
    return service


@router.get("/{module}/configuration")
def configuration(module: Module, request: Request) -> dict:
    """只返回当前连接模式与能力，不返回地址或凭据。"""
    settings = module_settings(request.app.state.settings, module, "huawei")
    return {
        "provider": settings.toolchain_provider,
        "mode": settings.toolchain_mode,
        "available": settings.toolchain_provider == "huawei"
        and settings.toolchain_mode != "disabled",
        "pollingEnabled": settings.toolchain_polling_enabled,
        "pollInterval": settings.toolchain_poll_interval,
        "importAvailable": module in ("applications", "datasets"),
        "entryUrl": module_entry_url(request.app.state.settings, module, "huawei"),
    }


@router.get("/{module}/providers")
def providers(module: Module, request: Request) -> dict:
    """列出两家独立接入状态；页面选择不会修改全局配置或回传凭据。"""
    settings = module_settings(request.app.state.settings, module, "aliyun")
    return {
        "aliyun": {
            "provider": "aliyun",
            "mode": settings.toolchain_mode,
            "available": settings.toolchain_mode != "disabled",
        },
        "huawei": configuration(module, request),
    }


@router.get("/{module}/resources")
def resources(
    module: Module,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    q: Annotated[str, Query(max_length=200)] = "",
) -> dict:
    """读取分页目录；后台未启用时首次通过明确刷新获取数据。"""
    return catalogs(request, module).page(module, page, page_size, q)


@router.post("/{module}/refresh")
def refresh(module: Module, request: Request) -> dict:
    """手动全量查询，不修改平台资源或发布申请。"""
    return catalogs(request, module).refresh(module, force=True)


@router.get("/{module}/resources/{identifier}")
def detail(module: Module, identifier: str, request: Request) -> dict:
    """读取当前供应方元数据，导入使用本次返回的快照令牌。"""
    service = catalogs(request, module)
    return HuaweiCatalog(service.clients[module]).detail(module, identifier)


@router.get("/knowledge/resources/{identifier}/documents")
def documents(
    identifier: str,
    request: Request,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
) -> dict:
    """查询外部文档列表；索引型知识库的 PageIndex 接口尚未交付。"""
    service = catalogs(request, "knowledge")
    provider = HuaweiCatalog(service.clients["knowledge"])
    item = provider.detail("knowledge", identifier)
    if not item["documentsAvailable"]:
        raise HTTPException(422, "当前索引型知识库未提供 PageIndex 文档查询接口")
    return provider.documents(identifier, page, page_size)


@router.post("/{module}/resources/{identifier}/import")
def import_draft(module: Module, identifier: str, payload: CatalogImport, request: Request) -> dict:
    """只导入语义已对应的智能体和数据集元数据，继续走平台发布审核。"""
    catalogs(request, module)
    actor = current_actor(request)
    if module == "applications":
        service = request.app.state.application_service
        return request.app.state.huawei_application_provider.import_draft(
            service, actor, identifier, payload.snapshot
        )
    if module == "datasets":
        return HuaweiDatasetToolchain(
            request.app.state.toolchain_catalogs, request.app.state.dataset_workspace
        ).import_draft(
            actor,
            identifier,
            payload.snapshot,
            publication_defaults(request, actor),
        )
    raise HTTPException(422, "当前资源仅支持外部目录查询")
