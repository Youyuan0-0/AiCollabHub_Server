"""为完整 HTTP 请求生命周期绑定系统日志上下文。"""

import logging
from uuid import uuid4

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.log_sanitizer import exception_details
from app.core.system_logging import system_log_context

logger = logging.getLogger(__name__)


class SystemLogContextMiddleware:
    """在路由、依赖及流式响应期间沿用同一请求编号，不读取请求正文。"""

    def __init__(self, app: ASGIApp) -> None:
        """保存下游 ASGI 应用，实际写入器从当前应用状态获取。"""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """记录未捕获异常并继续交给框架处理，保持既有失败语义。"""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4()
        scope.setdefault("state", {})["request_id"] = str(request_id)
        with system_log_context(scope["app"].state.system_log_writer, request_id):
            try:
                await self.app(scope, receive, send)
            except Exception as error:
                logger.error(
                    "Unhandled request exception (%s)",
                    type(error).__name__,
                    extra={
                        "source": "http",
                        "event": "request_unhandled_exception",
                        "details": {
                            **exception_details(error),
                            "method": scope["method"],
                            "route": getattr(scope.get("route"), "path", "<unmatched>"),
                        },
                    },
                )
                raise
