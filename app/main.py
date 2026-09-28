from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from os import getpid

from fastapi import Depends, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint

from app.api.router import api_router, database_router, mock_router
from app.core.config import Settings
from app.core.foundation_services import configure_foundation
from app.core.resources import ApplicationResources, create_resources
from app.core.system_log_middleware import SystemLogContextMiddleware
from app.core.system_logging import SystemLogWriter, capture_system_logs
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.demo_store import (
    create_demo_application_service,
)
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.iam import CasClient, IamIngressVerifier
from app.modules.common.auth.router import config_router
from app.modules.common.auth.router import router as auth_router
from app.modules.common.auth.service import require_business_identity
from app.modules.common.identity import DemoIdentityProvider, SessionIdentityProvider
from app.modules.common.identity_sync.iam_router import router as iam_sync_router
from app.modules.common.identity_sync.router import router as identity_sync_router
from app.modules.common.memory_reports import MemoryReports
from app.modules.common.resource_workspace import ResourceWorkspace
from app.modules.common.toolchain.assembly import configure_toolchains
from app.modules.common.toolchain.configuration import ToolchainConfigurationStore, module_settings
from app.modules.datasets.publishing import create_dataset_publisher
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace
from app.modules.feedback.maintenance import maintain_feedback
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.retrieval import RetrievalProvider
from app.modules.knowledge.service import KnowledgeService
from app.modules.knowledge.storage import KnowledgeFiles
from app.modules.knowledge.workspace import KnowledgeWorkspace, create_knowledge_provider
from app.modules.models.workspace import create_model_provider
from app.services.attachment_store import AttachmentStore, LocalAttachmentStore
from app.services.chat_provider import ChatProvider, EchoChatProvider
from app.services.home_activity import HomeActivityStore
from app.storage.client import ObjectNotFound, StorageUnavailable

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: Callable[[Settings], ApplicationResources] = create_resources,
    chat_provider: ChatProvider | None = None,
    attachment_store: AttachmentStore | None = None,
    cas_client: CasClient | None = None,
    iam_verifier: IamIngressVerifier | None = None,
    applications_factory: Callable[[], ApplicationService] | None = None,
) -> FastAPI:
    """装配独立的 FastAPI 实例，并在 lifespan 中管理外部资源。

    settings 决定环境、认证和存储模式；各 factory/provider 参数供测试或接入层
    替换具体实现。未注入的问答提供器默认回显问题，不代表已连接真实模型。
    正式 IAM 模式必须同时提供经过验证的 CAS 客户端和通知验签器。

    调用工厂只完成应用配置；连接池创建、现有表结构读取和后台任务启动发生在
    lifespan 阶段。数据库迁移须独立执行，启动过程不会补表或导入样例数据。
    """
    settings = settings if settings is not None else Settings()
    if settings.auth_mode == "iam" and (cas_client is None or iam_verifier is None):
        raise ValueError("IAM mode requires a verified CAS client and notification verifier")
    if settings.auth_mode == "iam" and isinstance(cas_client, MockCasClient):
        raise ValueError("Mock CAS client cannot be used in IAM mode")
    toolchain_configuration = ToolchainConfigurationStore(settings)
    settings = toolchain_configuration.settings(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """先停止业务后台任务，再排空日志队列，最后释放公共连接池。"""
        try:
            async with AsyncExitStack() as cleanup:
                # 同步客户端的创建和关闭放到线程池，避免阻塞 ASGI 事件循环。
                # 退出栈后进先出：先停止日志采集并排空队列，最后才释放数据库连接池。
                resources = await run_in_threadpool(resources_factory, settings)
                cleanup.push_async_callback(run_in_threadpool, resources.close)
                writer = SystemLogWriter(resources.database, settings.instance_id)
                cleanup.push_async_callback(run_in_threadpool, writer.close)
                cleanup.enter_context(capture_system_logs(writer))
                application.state.system_log_writer = writer
                application.state.resources = resources
                application.state.tutorial_upload_limiter = asyncio.Semaphore(2)
                application.state.feedback_upload_limiter = asyncio.Semaphore(
                    settings.feedback_upload_concurrency
                )
                application.state.database_engine = resources.database
                if (
                    resources.database is not None
                    and resources.database.dialect.name == "postgresql"
                ):
                    # 配置 PostgreSQL 后以真实仓储替换初始内存实现；反射失败直接中止启动。
                    # 显式注入的应用服务和附件存储保留，便于隔离契约测试的外部依赖。
                    await run_in_threadpool(
                        configure_foundation,
                        application,
                        resources,
                        settings,
                        configure_applications=applications_factory is None,
                        configure_attachments=attachment_store is None,
                    )
                # 工具链复用已装配的业务仓储；启动失败和正常退出都先关闭客户端再释放连接池。
                toolchain_clients = configure_toolchains(application)
                cleanup.callback(toolchain_clients.close)
                if (
                    settings.app_env != "production"
                    and settings.applications_runtime == "agent_dev"
                ):
                    from app.modules.applications.runtime import AgentDevProvider

                    service = application.state.application_service
                    service.provider = AgentDevProvider(
                        service.provider, module_settings(settings, "applications")
                    )
                    service.workflows.provider = service.provider
                application.state.cas_client = cas_client
                application.state.iam_verifier = iam_verifier
                if settings.auth_mode == "mock" and cas_client is None:
                    application.state.cas_client = MockCasClient(settings)
                    cleanup.callback(application.state.cas_client.close)
                feedback_stopping = asyncio.Event()
                feedback_maintenance = asyncio.create_task(
                    maintain_feedback(resources, settings, feedback_stopping)
                )
                try:
                    yield
                finally:
                    # 等清理协程结束后再退出资源栈，防止后台任务使用已经关闭的客户端。
                    feedback_stopping.set()
                    await feedback_maintenance
        finally:
            application.state.foundation = None
            application.state.system_log_writer = None
            application.state.database_engine = None
            application.state.resources = None

    # 生产环境关闭交互文档和 OpenAPI 描述，减少不必要的接口信息暴露。
    development_docs = settings.app_env != "production"
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if development_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development_docs else None,
    )
    # 环境和回复提供器由服务端配置，业务登录在挂载路由时统一校验。
    application.state.app_env = settings.app_env
    application.state.chat_provider = (
        chat_provider if chat_provider is not None else EchoChatProvider()
    )
    application.state.settings = settings
    application.state.toolchain_configuration = toolchain_configuration
    # 注入点隔离开发文件仓库，测试及未来 PostgreSQL/MinIO 适配器共用此合同。
    application.state.attachment_store = (
        attachment_store
        if attachment_store is not None
        else LocalAttachmentStore(settings.conversation_attachment_dir)
    )
    application.state.database_engine = None
    application.state.foundation = None
    # 每个应用实例独立保存首页公告和运营设置；不依赖数据库迁移。
    application.state.home_activity = HomeActivityStore()
    application.state.resources = None
    application.state.system_log_writer = None
    application.state.resource_reports = MemoryReports()
    application.state.model_provider = create_model_provider()
    application.state.model_workspace = ResourceWorkspace(
        "model", application.state.model_provider.list_models()
    )
    application.state.knowledge_workspace = KnowledgeWorkspace(create_knowledge_provider())

    @application.middleware("http")
    async def request_context(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """为请求和响应补充追踪标识与数据模式，并限制接口响应缓存。"""
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-App-Instance"] = settings.instance_id
        if settings.app_env != "production":
            response.headers["X-App-Worker"] = str(getpid())
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            if settings.app_env != "production" and not request.url.path.startswith(
                ("/api/v1/health/", "/api/v1/auth/")
            ):
                # 看板和会话包含持久化数据，不能标记为无状态 Mock；样例板块由响应元信息区分。
                database_path = request.url.path.split("/")[3:4]
                response.headers["X-Data-Mode"] = (
                    "development"
                    if database_path in (["overview"], ["conversations"], ["feedback"])
                    else "mock"
                )
                if request.url.path.startswith(
                    ("/api/v1/admin/feedback", "/api/v1/admin/overview", "/api/v1/admin/logs")
                ):
                    response.headers["X-Data-Mode"] = "development"
                scenario_path = request.url.path.removeprefix("/api/v1/").removeprefix("admin/")
                if (
                    scenario_path.startswith("application")
                    and settings.applications_storage == "database"
                ):
                    response.headers["X-Data-Mode"] = "development"
                    response.headers["X-Application-Storage"] = "postgresql"
                    response.headers["X-Application-Provider"] = settings.applications_runtime
                if scenario_path.startswith("dataset"):
                    response.headers["X-Dataset-Storage"] = settings.datasets_storage
                    # 非生产诊断标识实际发布通道，避免将本地上架误认为第三方已集成。
                    response.headers["X-Dataset-Publication-Mode"] = (
                        settings.datasets_publication_mode
                    )
                    if settings.datasets_storage == "database":
                        response.headers["X-Data-Mode"] = "development"
                if scenario_path.startswith("knowledge-bases"):
                    response.headers["X-Data-Mode"] = "development"
                    response.headers["X-Knowledge-Storage"] = settings.knowledge_storage
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers["Referrer-Policy"] = "no-referrer"
        if (
            request.url.path.startswith("/api/")
            and not request.url.path.startswith("/api/v1/auth/")
            and application.state.foundation is not None
        ):
            response.headers["X-Data-Mode"] = (
                "mock" if request.url.path.startswith("/api/v1/overview/operations") else "database"
            )
            for header in ("X-Application-Storage", "X-Dataset-Storage", "X-Knowledge-Storage"):
                if header in response.headers:
                    response.headers[header] = "postgresql"
        return response

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, error: SQLAlchemyError) -> JSONResponse:
        """将数据库异常转换为不含内部连接信息的服务错误。"""
        logger.warning(
            "Database request failed (%s, sqlstate=%s, request=%s)",
            type(error).__name__,
            getattr(getattr(error, "orig", None), "sqlstate", None),
            request.state.request_id,
            extra={
                "source": "database",
                "event": "database_request_failed",
                "details": {
                    "errorType": type(error).__name__,
                    "sqlState": getattr(getattr(error, "orig", None), "sqlstate", None),
                },
            },
        )
        return JSONResponse(status_code=503, content={"detail": "database_unavailable"})

    @application.exception_handler(StorageUnavailable)
    async def storage_unavailable(request: Request, error: StorageUnavailable) -> JSONResponse:
        """将存储访问失败转换为统一服务不可用响应。"""
        logger.warning(
            "Object storage unavailable",
            extra={"source": "storage", "event": "object_storage_unavailable"},
        )
        return JSONResponse(status_code=503, content={"detail": "object_storage_unavailable"})

    @application.exception_handler(ObjectNotFound)
    async def object_not_found(request: Request, error: ObjectNotFound) -> JSONResponse:
        """将存储对象缺失转换为统一资源不存在响应。"""
        return JSONResponse(status_code=404, content={"detail": "object_not_found"})

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> Response:
        """记录权限拒绝与服务失败，保留框架原有的状态码、响应体和响应头。"""
        if error.status_code == 403 or error.status_code >= 500:
            logger.warning(
                "Request failed (status=%s)",
                error.status_code,
                extra={
                    "source": "http",
                    "event": "request_denied" if error.status_code == 403 else "request_failed",
                    "details": {
                        "statusCode": error.status_code,
                        "method": request.method,
                        "route": getattr(request.scope.get("route"), "path", "<unmatched>"),
                    },
                },
            )
        return await http_exception_handler(request, error)

    @application.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception) -> JSONResponse:
        """未捕获异常已由日志中间件记录，此处仅返回无内部信息的响应。"""
        return JSONResponse(
            status_code=500,
            content={"detail": "internal_server_error"},
            headers={"X-Request-ID": request.state.request_id, "Cache-Control": "no-store"},
        )

    # Vue 使用同源开发代理时无需 CORS；独立跨域访问须显式配置允许的来源。
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Content-Type",
            ],
        )
    # 放在其他用户中间件外层，覆盖同步依赖、异常处理和流式响应的完整生命周期。
    application.add_middleware(SystemLogContextMiddleware)
    application.include_router(api_router)
    # 持久化接口与 Mock 业务共用认证开关，健康检查和登录入口保持独立。
    business_dependencies = (
        [Depends(require_business_identity)] if settings.auth_mode != "disabled" else []
    )
    application.include_router(database_router, dependencies=business_dependencies)
    application.include_router(config_router, prefix="/api/v1")
    if settings.auth_mode != "disabled":
        application.include_router(auth_router, prefix="/api/v1")
    if settings.auth_mode == "mock":
        application.include_router(identity_sync_router, prefix="/api/v1")
    if settings.auth_mode == "iam":
        application.include_router(iam_sync_router, prefix="/api/v1")
    if settings.auth_mode != "disabled":
        application.state.application_identity = SessionIdentityProvider()
    # 认证按配置启用；生产环境不提供模拟 IAM 或样例业务。
    if settings.app_env != "production":
        from app.modules.tools.workspace import ToolWorkspace

        application.state.tool_workspace = ToolWorkspace()
        # 与其他资源模块保持相同环境边界；内存模式只供开发，重启后资料不保留。
        application.state.knowledge_service = KnowledgeService(
            KnowledgeRepository(),
            KnowledgeFiles(settings.knowledge_document_dir),
            RetrievalProvider(settings),
        )
        # 每个实例独立的演示状态；数据库模式在 lifespan 中替换为共享仓储。
        application.state.dataset_workspace = DatasetWorkspace(
            DatasetRepository(), create_dataset_publisher(settings.datasets_publication_mode)
        )
        # 每个 FastAPI 实例创建一份场景应用业务对象，路由从 request.app.state 取出并复用。
        application.state.application_service = (
            applications_factory or create_demo_application_service
        )()
        if settings.auth_mode == "disabled":
            application.state.application_identity = DemoIdentityProvider()
        application.include_router(
            mock_router,
            dependencies=business_dependencies,
        )
    return application


app = create_app()
