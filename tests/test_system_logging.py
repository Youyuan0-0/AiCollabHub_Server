import json
import logging
from threading import Event
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_auth import auth as auth
from test_auth import login
from test_identity_sync import batch, push

from app.core.config import Settings
from app.core.log_sanitizer import redact_text, sanitize_value
from app.core.system_logging import SystemLogWriter, capture_system_logs
from app.db.base import Base
from app.db.session import SessionDependency, create_db_engine
from app.main import create_app
from app.modules.common.auth.models import OperationLog, User
from app.modules.common.system_logs import SystemLog

logger = logging.getLogger("app.modules.common.logging_test")


@pytest.fixture
def log_engine(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'logs.db').as_posix()}")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_critical_events_use_error_level_without_losing_evidence(log_engine):
    """严重异常统一为 ERROR，普通调试仍不入库，原始消息不丢失。"""
    writer = SystemLogWriter(log_engine, "three-level-test")
    try:
        with capture_system_logs(writer):
            logger.debug("Debug event is not persisted")
            logger.info("Information event")
            logger.warning("Warning event")
            logger.error("Error event")
            logger.critical("Critical event", extra={"event": "critical_probe"})
    finally:
        writer.close()
    with Session(log_engine) as session:
        records = session.scalars(select(SystemLog).order_by(SystemLog.created_at)).all()
        assert [record.level for record in records] == ["info", "warning", "error", "error"]
        assert records[-1].message == "Critical event"
        assert records[-1].event == "critical_probe"


def test_auth_failures_and_sync_conflicts_are_persisted_after_rollback(auth):
    a, _, iam, engine = auth
    denied = login(a, iam, scope="admin")
    with Session(engine) as session:
        # 普通用户仍可建立共享会话；被拒绝的是管理端准入，成功登录保留操作记录。
        operation = session.scalar(select(OperationLog))
        assert operation is not None and operation.action == "login"
        assert operation.data == {"entryScope": "admin"}
    applied = push(a, batch("logging-user", "active", 100))
    conflict = push(a, batch("logging-user", "disabled", 100))
    assert applied.status_code == 200 and conflict.status_code == 409
    # 停止采集并排空，确定异步事务已经完成，不用 sleep 猜测落库时机。
    a.app.state.system_log_writer.close()
    with Session(engine) as session:
        records = session.scalars(select(SystemLog)).all()
        by_request = {record.request_id: record for record in records}
        login_log = next(
            record
            for record in records
            if record.request_id == UUID(denied.headers["X-Request-ID"])
            and record.event == "auth_login_failed"
        )
        assert login_log.event == "auth_login_failed"
        assert login_log.details["result"] == "admin_not_allowed"
        assert by_request[UUID(applied.headers["X-Request-ID"])].event == "identity_sync_applied"
        assert by_request[UUID(conflict.headers["X-Request-ID"])].event == "identity_sync_failed"
        assert (
            session.scalar(select(User).where(User.iam_subject == "logging-user")).status
            == "active"
        )


def test_request_rollback_does_not_remove_system_log(log_engine):
    app = create_app(Settings(database_url=str(log_engine.url)))

    @app.post("/test-rollback")
    def rollback(session: SessionDependency):
        session.add(User(iam_subject="must-rollback"))
        session.flush()
        logger.warning(
            "Business transaction will be rolled back", extra={"event": "rollback_probe"}
        )
        raise HTTPException(409, "test_conflict")

    with TestClient(app) as client:
        response = client.post("/test-rollback")
        assert response.status_code == 409
    with Session(log_engine) as session:
        assert session.scalar(select(User)) is None
        record = session.scalar(select(SystemLog).where(SystemLog.event == "rollback_probe"))
        assert record.request_id == UUID(response.headers["X-Request-ID"])


def test_unhandled_and_streaming_exceptions_keep_request_id_without_secrets(log_engine):
    app = create_app(Settings(database_url=str(log_engine.url)))

    @app.get("/test-unhandled")
    def unhandled():
        raise RuntimeError("private-password-in-exception")

    @app.get("/test-stream")
    def stream():
        def chunks():
            yield b"started"
            raise RuntimeError("private-stream-ticket")

        return StreamingResponse(chunks())

    with TestClient(app, raise_server_exceptions=False) as client:
        failed = client.get("/test-unhandled?ticket=private-query-ticket")
        streamed = client.get("/test-stream")
        assert failed.status_code == 500
        assert failed.json() == {"detail": "internal_server_error"}
    with Session(log_engine) as session:
        records = session.scalars(select(SystemLog)).all()
        assert len(records) == 2
        assert {record.request_id for record in records} == {
            UUID(failed.headers["X-Request-ID"]),
            UUID(streamed.headers["X-Request-ID"]),
        }
        assert all(record.event == "request_unhandled_exception" for record in records)
        assert all(record.details["frames"] for record in records)
        assert "private-" not in json.dumps([record.details for record in records])


def test_records_are_sanitized_bounded_and_do_not_retain_mutable_context(log_engine):
    details = {
        "accessToken": "private-access-token",
        "nested": {"headers": {"Authorization": "Bearer private-header"}},
        "body": {"user": "private-user"},
        "description": "password=private-inline-secret",
        "payload": "private-body",
        "url": "postgresql://user:private-password@internal/database?ticket=private-ticket",
        "safeValue": "initial",
    }
    writer = SystemLogWriter(log_engine, "test")
    try:
        with capture_system_logs(writer):
            logger.warning(
                "Call failed password=private-password url=https://private.example?token=private-token",
                extra={"event": "sanitization_probe", "details": details},
            )
            details["safeValue"] = "changed"
            try:
                raise ValueError("private-exception-text")
            except ValueError:
                logger.exception("Provider failed", extra={"event": "exception_probe"})
            logger.info("Huge context", extra={"details": {str(i): "x" * 2000 for i in range(30)}})
    finally:
        writer.close()
    with Session(log_engine) as session:
        records = session.scalars(select(SystemLog).order_by(SystemLog.created_at)).all()
        assert len(records) == 3
        assert all(record.request_id is None for record in records)
        assert "private-" not in json.dumps(
            [{"message": record.message, "details": record.details} for record in records]
        )
        assert records[0].details["safeValue"] == "initial"
        assert records[1].details["errorType"] == "ValueError"
        assert records[2].details["truncated"] is True


def test_sanitizer_bounds_nested_input_and_removes_invalid_database_characters():
    assert "\x00" not in redact_text("line\x00value\ud800")
    assert redact_text("Cookie: session=private-session; second=private-second") == "[REDACTED]"
    nested = "x" * 2000
    for _ in range(4):
        nested = {str(index): nested for index in range(30)}
    assert len(json.dumps(sanitize_value(nested))) < 250000


def test_full_queue_does_not_block_request_and_retains_console_fallback(
    log_engine, monkeypatch, caplog
):
    started, release = Event(), Event()
    original = SystemLogWriter._write

    def stalled(self, rows):
        started.set()
        assert release.wait(timeout=5)
        original(self, rows)

    monkeypatch.setattr(SystemLogWriter, "_write", stalled)
    writer = SystemLogWriter(log_engine, "test", queue_capacity=1)
    try:
        with capture_system_logs(writer):
            logger.warning("First event")
            assert started.wait(timeout=2)
            logger.warning("Queued event")
            logger.warning("Overflow event")
            assert "queue_full" in caplog.text
    finally:
        release.set()
        writer.close()
    with Session(log_engine) as session:
        assert len(session.scalars(select(SystemLog)).all()) == 2


def test_database_failure_falls_back_without_recursion_then_recovers(
    log_engine, monkeypatch, caplog
):
    from app.core import system_logging

    original = SystemLogWriter._write
    failed = Event()

    def fail_once(self, rows):
        if not failed.is_set():
            failed.set()
            raise RuntimeError("private-database-password")
        original(self, rows)

    monkeypatch.setattr(SystemLogWriter, "_write", fail_once)
    monkeypatch.setattr(system_logging, "RETRY_INTERVAL", 0)
    writer = SystemLogWriter(log_engine, "test")
    try:
        with capture_system_logs(writer):
            logger.error("Database failure probe")
            assert failed.wait(timeout=2)
            logger.info("Recovered event", extra={"event": "recovery_probe"})
    finally:
        writer.close()
    assert "not persisted (RuntimeError)" in caplog.text
    assert "private-database-password" not in caplog.text
    with Session(log_engine) as session:
        records = session.scalars(select(SystemLog)).all()
        assert len(records) == 1 and records[0].event == "recovery_probe"


def test_multiple_apps_and_threadpool_requests_do_not_share_log_context(tmp_path):
    engines = [
        create_db_engine(f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}") for name in ("A", "B")
    ]
    clients = []
    application_logger = logging.getLogger("app")
    previous_handlers = list(application_logger.handlers)
    previous_level = application_logger.level
    try:
        for index, engine in enumerate(engines):
            Base.metadata.create_all(engine)
            app = create_app(Settings(database_url=str(engine.url), instance_id=f"api{index}"))

            @app.get("/test-context")
            def log_context():
                logger.info("Context probe", extra={"event": "context_probe"})
                return {"ok": True}

            clients.append(TestClient(app))
        with clients[0] as a, clients[1] as b:
            responses = [a.get("/test-context"), b.get("/test-context")]
        for index, engine in enumerate(engines):
            with Session(engine) as session:
                records = session.scalars(select(SystemLog)).all()
                assert len(records) == 1
                assert records[0].details["instanceId"] == f"api{index}"
                assert records[0].request_id == UUID(responses[index].headers["X-Request-ID"])
        assert application_logger.handlers == previous_handlers
        assert application_logger.level == previous_level
    finally:
        for engine in engines:
            engine.dispose()
