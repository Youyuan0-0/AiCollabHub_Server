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
from app.core.resources import ApplicationResources, create_resources
from app.core.system_log_middleware import SystemLogContextMiddleware
from app.core.system_logging import SystemLogWriter, capture_system_logs
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.demo_store import (
    create_database_application_service,
    create_demo_application_service,
)
from app.modules.applications.identity import DemoIdentityProvider, SessionIdentityProvider
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.router import config_router
from app.modules.common.auth.router import router as auth_router
from app.modules.common.auth.service import require_business_identity
from app.modules.common.identity_sync.router import router as identity_sync_router
from app.modules.datasets.publishing import create_dataset_publisher
from app.modules.datasets.repository import DatasetRepository
from app.modules.datasets.workspace import DatasetWorkspace
from app.modules.feedback.maintenance import maintain_feedback
from app.services.attachment_store import AttachmentStore, LocalAttachmentStore
from app.services.chat_provider import ChatProvider, EchoChatProvider
from app.storage.client import ObjectNotFound, StorageUnavailable

logger = logging.getLogger(__name__)


# settings 与 resources_factory 隔离实例配置和连接池；chat_provider 可注入回复实现，默认回显。
# 返回独立 FastAPI 应用，启动仅创建资源客户端，不自动执行数据库迁移。
def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: Callable[[Settings], ApplicationResources] = create_resources,
    chat_provider: ChatProvider | None = None,
    attachment_store: AttachmentStore | None = None,
    cas_client: MockCasClient | None = None,
    applications_factory: Callable[[], ApplicationService] | None = None,
) -> FastAPI:
    """装配应用路由、认证与资源生命周期，支持开发和测试注入服务。"""
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """先停止业务后台任务，再排空日志队列，最后释放公共连接池。"""
        try:
            async with AsyncExitStack() as cleanup:
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
                # 数据集与应用复用单库连接池，但申请及活动记录独立存储；启动时不自动建表。
                if settings.app_env != "production" and settings.datasets_storage == "database":
                    application.state.dataset_workspace = DatasetWorkspace(
                        DatasetRepository(resources.database),
                        create_dataset_publisher(settings.datasets_publication_mode),
                    )
                if (
                    settings.app_env != "production"
                    and applications_factory is None
                    and settings.applications_storage == "database"
                ):
                    application.state.application_service = create_database_application_service(
                        application.state.database_engine, settings
                    )
                if (
                    settings.app_env != "production"
                    and settings.applications_runtime == "agent_dev"
                ):
                    from app.modules.applications.runtime import AgentDevProvider

                    service = application.state.application_service
                    service.provider = AgentDevProvider(service.provider, settings)
                    service.workflows.provider = service.provider
                application.state.cas_client = cas_client
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
                    feedback_stopping.set()
                    await feedback_maintenance
        finally:
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
    # 注入点隔离开发文件仓库，测试及未来 PostgreSQL/MinIO 适配器共用此合同。
    application.state.attachment_store = (
        attachment_store
        if attachment_store is not None
        else LocalAttachmentStore(settings.conversation_attachment_dir)
    )
    application.state.database_engine = None
    application.state.resources = None
    application.state.system_log_writer = None

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
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers["Referrer-Policy"] = "no-referrer"
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
        [Depends(require_business_identity)] if settings.auth_mode == "mock" else []
    )
    application.include_router(database_router, dependencies=business_dependencies)
    application.include_router(config_router, prefix="/api/v1")
    if settings.auth_mode == "mock":
        application.include_router(auth_router, prefix="/api/v1")
        application.include_router(identity_sync_router, prefix="/api/v1")
    # 认证按配置启用；生产环境不提供模拟 IAM 或样例业务。
    if settings.app_env != "production":
        # 每个实例独立的演示状态；数据库模式在 lifespan 中替换为共享仓储。
        application.state.dataset_workspace = DatasetWorkspace(
            DatasetRepository(), create_dataset_publisher(settings.datasets_publication_mode)
        )
        # 每个 FastAPI 实例创建一份场景应用业务对象，路由从 request.app.state 取出并复用。
        application.state.application_service = (
            applications_factory or create_demo_application_service
        )()
        application.state.application_identity = (
            SessionIdentityProvider() if settings.auth_mode == "mock" else DemoIdentityProvider()
        )
        application.include_router(
            mock_router,
            dependencies=business_dependencies,
        )
    return application


app = create_app()
