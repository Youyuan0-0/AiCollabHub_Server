from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router, mock_router
from app.core.config import Settings
from app.core.resources import ApplicationResources, create_resources
from app.modules.applications.application_service import ApplicationService
from app.modules.applications.demo_store import create_demo_application_service
from app.modules.applications.identity import DemoIdentityProvider, SessionIdentityProvider
from app.modules.common.auth.cas import MockCasClient
from app.modules.common.auth.router import config_router
from app.modules.common.auth.router import router as auth_router
from app.modules.common.auth.service import require_business_identity
from app.storage.client import ObjectNotFound, StorageUnavailable


# 工厂函数允许测试注入独立配置，避免不同应用实例共享数据库连接池。
def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: Callable[[Settings], ApplicationResources] = create_resources,
    cas_client=None,
    applications_factory: Callable[[], ApplicationService] = create_demo_application_service,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resources = await run_in_threadpool(resources_factory, settings)
        application.state.resources = resources
        application.state.database_engine = (
            resources.database.primary if resources.database is not None else None
        )
        application.state.cas_client = cas_client
        if settings.auth_mode == "mock" and cas_client is None:
            application.state.cas_client = MockCasClient(settings)
        try:
            yield
        finally:
            if settings.auth_mode == "mock" and cas_client is None:
                application.state.cas_client.close()
            try:
                await run_in_threadpool(resources.close)
            finally:
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
    application.state.settings = settings
    application.state.database_engine = None
    application.state.resources = None

    @application.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-App-Instance"] = settings.instance_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            if settings.app_env != "production" and not request.url.path.startswith(
                ("/api/v1/health/", "/api/v1/auth/")
            ):
                response.headers["X-Data-Mode"] = "mock"
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "database_unavailable"})

    @application.exception_handler(StorageUnavailable)
    async def storage_unavailable(request, error):
        return JSONResponse(status_code=503, content={"detail": "object_storage_unavailable"})

    @application.exception_handler(ObjectNotFound)
    async def object_not_found(request, error):
        return JSONResponse(status_code=404, content={"detail": "object_not_found"})

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
    application.include_router(api_router)
    application.include_router(config_router, prefix="/api/v1")
    if settings.auth_mode == "mock":
        application.include_router(auth_router, prefix="/api/v1")
    # 认证按配置启用，业务仍返回 Mock；生产环境不提供模拟 IAM 或样例业务。
    if settings.app_env != "production":
        # 每个 FastAPI 实例创建一份场景应用业务对象，路由从 request.app.state 取出并复用。
        application.state.application_service = applications_factory()
        application.state.application_identity = (
            SessionIdentityProvider() if settings.auth_mode == "mock" else DemoIdentityProvider()
        )
        application.include_router(
            mock_router,
            dependencies=(
                [Depends(require_business_identity)] if settings.auth_mode == "mock" else []
            ),
        )
    return application


app = create_app()
