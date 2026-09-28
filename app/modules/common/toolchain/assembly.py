"""各模块工具链统一装配；连接凭据和业务仓储分别管理。"""

from contextlib import ExitStack

import httpx
from fastapi import FastAPI

from app.modules.applications.dependencies import platform_dependencies
from app.modules.applications.providers import DemoApplicationProvider
from app.modules.applications.toolchain import AgentCatalogProvider
from app.modules.common.toolchain.client import PopClient
from app.modules.common.toolchain.configuration import module_settings
from app.modules.common.toolchain.configuration_schemas import MODULE_NAMES
from app.modules.common.toolchain.mock import DocumentedToolchainMock
from app.modules.knowledge.engine import KnowledgeEngineProvider
from app.modules.knowledge.engine_workspace import EngineKnowledgeWorkspace
from app.modules.models.toolchain import StudioModelProvider
from app.modules.tools.toolchain import AgentToolCatalogProvider
from app.storage.client import MinioStorage


def configure_toolchains(application: FastAPI) -> ExitStack:
    """按模块配置一次性装配工具链，返回由应用生命周期负责关闭的退出栈。

    模型、应用、工具等模块可以各自选择真实、模拟或禁用模式；模拟客户端必须
    配套本地 transport，不能意外访问网络。这里替换外部提供器，正式业务仓储
    仍由基础服务装配维护。任一模块初始化失败都会释放本轮已创建的客户端。
    """
    settings = application.state.settings
    clients, mocks = {}, {}
    service = getattr(application.state, "application_service", None)
    if (
        settings.app_env != "production"
        and service
        and type(service.provider) is DemoApplicationProvider
    ):
        # 应用依赖读取本地知识库审批仓储，外部引擎适配器不得替换平台知识库目录。
        service.dependency_resolver = lambda actor: platform_dependencies(
            actor,
            application.state.dataset_workspace,
            application.state.knowledge_service,
            application.state.model_workspace,
            application.state.tool_workspace,
        )
    legacy_mock = DocumentedToolchainMock() if not settings.toolchain_module_connections else None
    with ExitStack() as cleanup:
        for module in MODULE_NAMES:
            specific = module_settings(settings, module)
            mock = (
                (legacy_mock or DocumentedToolchainMock())
                if specific.toolchain_mode == "mock"
                else None
            )
            client = PopClient(
                specific,
                httpx.MockTransport(mock) if mock else None,
                api_version="2024-06-24" if module == "models" else "2025-11-10",
                pop_code="aipaas" if module == "models" else "sfm",
            )
            cleanup.callback(client.close)
            clients[module], mocks[module] = client, mock
        if clients["models"].mode == "live":
            application.state.model_provider = StudioModelProvider(clients["models"])
        if clients["applications"].mode != "disabled" and settings.app_env != "production":
            service = application.state.application_service
            service.provider = AgentCatalogProvider(service.provider, clients["applications"])
            service.workflows.provider = service.provider
        if clients["tools"].mode == "live" and settings.app_env != "production":
            workspace = application.state.tool_workspace
            workspace.provider = AgentToolCatalogProvider(workspace.provider, clients["tools"])
        if (
            clients["knowledge"].mode != "disabled"
            and settings.app_env != "production"
            and getattr(application.state, "foundation", None) is None
        ):
            application.state.knowledge_workspace = EngineKnowledgeWorkspace(
                KnowledgeEngineProvider(clients["knowledge"]), mocks["knowledge"]
            )
        storage = settings.toolchain_knowledge_storage
        application.state.knowledge_storage = application.state.resources.storage
        if storage and storage["mode"] == "disabled":
            application.state.knowledge_storage = None
        elif storage and storage["mode"] == "dedicated":
            storage_settings = settings.model_copy(
                update={
                    "minio_endpoint": storage["endpoint"],
                    "minio_bucket": storage["bucket"],
                    "minio_region": storage["region"],
                    "minio_timeout": storage["timeout"],
                    "minio_access_key": storage["access_key"],
                    "minio_secret_key": storage["secret_key"],
                    "minio_max_upload_bytes": 100 * 1024 * 1024,
                }
            )
            dedicated_storage = MinioStorage.from_settings(
                storage_settings,
                proxy_url=str(storage["proxy_url"]) if storage.get("proxy_url") else None,
                ca_certificate=storage.get("ca_certificate", ""),
            )
            cleanup.callback(dedicated_storage.close)
            application.state.knowledge_storage = dedicated_storage
        application.state.toolchain_clients = clients
        application.state.toolchain_mocks = mocks
        # 旧诊断入口兼容数据集连接；业务调用全部按模块取值。
        application.state.toolchain_client = clients["datasets"]
        application.state.toolchain_mock = mocks["datasets"]
        return cleanup.pop_all()
