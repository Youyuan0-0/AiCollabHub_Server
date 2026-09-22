"""三类工具链统一装配；配置切换传输，业务请求和字段转换保持一致。"""

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
from app.storage.client import MinioStorage


def configure_toolchains(application: FastAPI) -> ExitStack:
    """分别装配三类连接，任一初始化失败时释放此前创建的客户端。"""
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
            client = PopClient(specific, httpx.MockTransport(mock) if mock else None)
            cleanup.callback(client.close)
            clients[module], mocks[module] = client, mock
        if clients["applications"].mode != "disabled" and settings.app_env != "production":
            service = application.state.application_service
            service.provider = AgentCatalogProvider(service.provider, clients["applications"])
            service.workflows.provider = service.provider
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
