from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.auth import IdentityProvider
from app.core.config import Settings
from app.db.session import create_db_engine
from app.services.chat_provider import ChatProvider, EchoChatProvider


# 工厂函数允许测试注入独立配置，避免不同应用实例共享数据库连接池。
def create_app(
    settings: Settings | None = None,
    *,
    identity_provider: IdentityProvider | None = None,
    chat_provider: ChatProvider | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # 在应用生命周期内管理连接池：yield 前初始化，应用退出时由 finally 清理。
        engine = (
            create_db_engine(settings.database_url.get_secret_value())
            if settings.database_url is not None
            else None
        )
        application.state.database_engine = engine
        try:
            yield
        finally:
            if engine is not None:
                # 引擎清理是同步操作，在线程池执行以免阻塞事件循环。
                await run_in_threadpool(engine.dispose)
            application.state.database_engine = None

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
    # 通过应用工厂注入受信任的 SSO 实现，默认不启用任何演示身份。
    application.state.identity_provider = identity_provider
    # 默认回显回复；第三方适配器通过工厂注入，不直接处理数据库。
    application.state.chat_provider = (
        chat_provider if chat_provider is not None else EchoChatProvider()
    )
    # 开发看板只读策略由服务端环境决定，不能由浏览器请求参数切换。
    application.state.app_env = settings.app_env
    # Vue 使用同源开发代理时无需 CORS；独立跨域访问须显式配置允许的来源。
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )
    application.include_router(api_router)
    return application


app = create_app()
