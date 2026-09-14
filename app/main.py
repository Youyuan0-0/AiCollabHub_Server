from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.router import api_router, mock_router
from app.core.config import Settings
from app.core.resources import ApplicationResources, create_resources
from app.services.chat_provider import ChatProvider, EchoChatProvider
from app.storage.client import ObjectNotFound, StorageUnavailable


# settings 与 resources_factory 隔离实例配置和连接池；chat_provider 可注入回复实现，默认回显。
# 返回独立 FastAPI 应用，启动仅创建资源客户端，不自动执行数据库迁移。
def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: Callable[[Settings], ApplicationResources] = create_resources,
    chat_provider: ChatProvider | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resources = await run_in_threadpool(resources_factory, settings)
        application.state.resources = resources
        application.state.database_engine = (
            resources.database.primary if resources.database is not None else None
        )
        try:
            yield
        finally:
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
    # 环境由服务端配置决定；会话回复提供器不依赖已移除的身份模块。
    application.state.app_env = settings.app_env
    application.state.chat_provider = (
        chat_provider if chat_provider is not None else EchoChatProvider()
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
                "/api/v1/health/"
            ):
                # 看板和会话包含持久化数据，不能标记为无状态 Mock；样例板块由响应元信息区分。
                database_path = request.url.path.split("/")[3:4]
                response.headers["X-Data-Mode"] = (
                    "development" if database_path in (["overview"], ["conversations"]) else "mock"
                )
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
    # 本阶段没有登录或权限模块；样例接口只在开发/测试环境注册。
    if settings.app_env != "production":
        application.include_router(mock_router)
    return application


app = create_app()
