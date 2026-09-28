"""以有界内存队列和独立事务保存应用日志，保留原有日志输出。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import Engine, insert, text

from app.core.log_sanitizer import (
    MAX_DETAILS_LENGTH,
    exception_details,
    redact_text,
    sanitize_value,
)
from app.modules.common.system_logs import SystemLog

QUEUE_CAPACITY = 1000
BATCH_SIZE = 50
SHUTDOWN_TIMEOUT = 10
RETRY_INTERVAL = 5
fallback_logger = logging.getLogger("system_log_fallback")
active_writer: ContextVar[SystemLogWriter | None] = ContextVar("system_log_writer", default=None)
active_request_id: ContextVar[UUID | None] = ContextVar("system_log_request_id", default=None)
_installation_lock = Lock()
_installation_count = 0
_previous_level = logging.NOTSET
_console_handler: logging.Handler | None = None


def _identifier(value: object, fallback: str, limit: int) -> str:
    """事件与来源仅接受代码标识，防止把任意请求内容用作分类。"""
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", value):
        return value[:limit]
    return fallback[:limit]


def _source(logger_name: str) -> str:
    """按既有模块目录归类来源，不要求各调用方重复填写模块名。"""
    parts = logger_name.split(".")
    if parts[:3] == ["app", "modules", "common"]:
        return parts[3] if len(parts) > 3 else "common"
    if parts[:2] == ["app", "modules"]:
        return parts[2] if len(parts) > 2 else "common"
    return parts[1] if len(parts) > 1 else "common"


def _snapshot(record: logging.LogRecord, writer: SystemLogWriter) -> dict[str, object]:
    """入队前提取不可变快照，队列不持有 Request、异常对象或原始上下文。"""
    message = record.msg if isinstance(record.msg, str) else f"[{type(record.msg).__name__}]"
    if record.args:
        arguments = sanitize_value(record.args)
        try:
            message %= tuple(arguments) if isinstance(arguments, list) else arguments
        except (TypeError, ValueError, KeyError):
            message = "Log message formatting failed"
    details = sanitize_value(getattr(record, "details", {}))
    details = details if isinstance(details, dict) else {}
    if len(json.dumps(details, ensure_ascii=False).encode("utf-8")) > MAX_DETAILS_LENGTH:
        details = {"truncated": True}
    details.update(logger=record.name, instanceId=writer.instance_id)
    if record.exc_info and record.exc_info[1]:
        details.update(exception_details(record.exc_info[1]))
    request_id = active_request_id.get()
    if request_id is None:
        try:
            value = getattr(record, "request_id", None)
            request_id = UUID(str(value)) if value is not None else None
        except ValueError:
            pass
    level = "info"
    # 严重错误统一记为 ERROR；标准日志输出仍保留原始 CRITICAL 级别。
    for threshold, name in (
        (logging.ERROR, "error"),
        (logging.WARNING, "warning"),
    ):
        if record.levelno >= threshold:
            level = name
            break
    return {
        "id": uuid4(),
        "level": level,
        "source": _identifier(getattr(record, "source", None), _source(record.name), 64),
        "event": _identifier(getattr(record, "event", None), f"runtime_{level}", 100),
        "message": redact_text(message),
        "request_id": request_id,
        "details": details,
        "created_at": datetime.fromtimestamp(record.created, UTC),
    }


class SystemLogWriter:
    """将已脱敏的系统日志从请求线程转交给独立写线程。

    每个应用实例持有有界队列；日志写入使用独立短事务，业务回滚不会撤销诊断记录。
    入队不等待空位，队列满、数据库故障或停机超时均降级到控制台。该内存队列
    不是持久化消息队列，进程被强制终止时仍可能丢失尚未处理的日志。
    """

    def __init__(
        self, engine: Engine | None, instance_id: str, *, queue_capacity: int = QUEUE_CAPACITY
    ) -> None:
        """只创建内存资源；没有日志时不访问数据库，不自动建表。"""
        if queue_capacity < 1:
            raise ValueError("System log queue capacity must be positive")
        self.engine = engine
        self.instance_id = instance_id
        self.queue: Queue[dict[str, object]] = Queue(maxsize=queue_capacity)
        self._stopping = Event()
        self._retry_after = 0.0
        self._shutdown_deadline: float | None = None
        self._thread = Thread(target=self._run, name="system-log-writer", daemon=True)
        if engine is not None:
            self._thread.start()

    def enqueue(self, record: logging.LogRecord) -> None:
        """非阻塞入队；未配置数据库时沿用标准输出，满队列时明确报告降级。"""
        if self.engine is None:
            return
        row = _snapshot(record, self)
        try:
            if self._stopping.is_set():
                self._fallback([row], "writer_stopping")
                return
            self.queue.put_nowait(row)
        except Full:
            self._fallback([row], "queue_full")

    def close(self) -> None:
        """停机时停止接收并限时排空；数据库故障不应无限阻止服务退出。"""
        if self._shutdown_deadline is None:
            self._shutdown_deadline = monotonic() + SHUTDOWN_TIMEOUT
        self._stopping.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=SHUTDOWN_TIMEOUT)
            if self._thread.is_alive():
                fallback_logger.warning(
                    "System log writer shutdown timed out; console logs retained"
                )

    def _fallback(self, rows: list[dict[str, object]], reason: str) -> None:
        """通过独立 logger 输出已脱敏快照，避免写库失败递归产生日志。"""
        fallback_logger.warning(
            "System logs not persisted (%s): %s",
            reason,
            json.dumps(rows, ensure_ascii=False, default=str),
        )

    def _write(self, rows: list[dict[str, object]]) -> None:
        """用独立短事务批量写入；不自动重试结果不确定的提交。"""
        if self.engine is None:
            return
        with self.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                connection.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            connection.execute(insert(SystemLog.__table__), rows)

    def _run(self) -> None:
        """批量消费并在数据库异常后短暂退避，故障期间保留控制台记录。"""
        while not self._stopping.is_set() or not self.queue.empty():
            try:
                rows = [self.queue.get(timeout=0.2)]
            except Empty:
                continue
            while len(rows) < BATCH_SIZE:
                try:
                    rows.append(self.queue.get_nowait())
                except Empty:
                    break
            try:
                if self._shutdown_deadline is not None and monotonic() >= self._shutdown_deadline:
                    self._fallback(rows, "shutdown_timeout")
                elif monotonic() < self._retry_after:
                    # 退避期内的批次直接输出到控制台，不积压重试队列，避免故障持续占用内存。
                    self._fallback(rows, "database_backoff")
                else:
                    self._write(rows)
            except Exception as error:
                self._retry_after = monotonic() + RETRY_INTERVAL
                self._fallback(rows, type(error).__name__)
            finally:
                for _ in rows:
                    self.queue.task_done()


class SystemLogHandler(logging.Handler):
    """根据上下文分派应用日志，隔离同进程中的多个 FastAPI 实例。"""

    def emit(self, record: logging.LogRecord) -> None:
        """采集失败仅报告异常类型，不影响业务请求或打印原始记录。"""
        writer = active_writer.get()
        if writer is None:
            return
        try:
            writer.enqueue(record)
        except Exception as error:
            fallback_logger.warning("System log capture failed (%s)", type(error).__name__)


_database_handler = SystemLogHandler(level=logging.INFO)


@contextmanager
def system_log_context(
    writer: SystemLogWriter | None, request_id: UUID | None = None
) -> Iterator[None]:
    """绑定当前应用和请求，兼容 asyncio 任务及 FastAPI 的线程池上下文传递。"""
    writer_token = active_writer.set(writer)
    request_token = active_request_id.set(request_id)
    try:
        yield
    finally:
        active_request_id.reset(request_token)
        active_writer.reset(writer_token)


@contextmanager
def capture_system_logs(writer: SystemLogWriter) -> Iterator[None]:
    """生命周期内安装一次采集器，最后一个实例退出后恢复原日志配置。"""
    global _installation_count, _previous_level, _console_handler
    application_logger = logging.getLogger("app")
    with _installation_lock:
        if _installation_count == 0:
            _previous_level = application_logger.level
            # Uvicorn 默认只配置自己的 logger；应用没有输出处理器时补充 stderr。
            if not application_logger.hasHandlers():
                _console_handler = logging.StreamHandler()
                _console_handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
                )
                application_logger.addHandler(_console_handler)
            application_logger.addHandler(_database_handler)
            if application_logger.getEffectiveLevel() > logging.INFO:
                application_logger.setLevel(logging.INFO)
        _installation_count += 1
    try:
        with system_log_context(writer):
            yield
    finally:
        with _installation_lock:
            _installation_count -= 1
            if _installation_count == 0:
                application_logger.removeHandler(_database_handler)
                application_logger.setLevel(_previous_level)
                if _console_handler is not None:
                    application_logger.removeHandler(_console_handler)
                    _console_handler.close()
                    _console_handler = None
