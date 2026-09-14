import logging
from threading import Event, Timer

import psycopg
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings

logger = logging.getLogger(__name__)


def prepare_write(session: Session, settings: Settings) -> None:
    if session.bind.dialect.name != "postgresql":
        return
    session.info["commit_timeout_ms"] = settings.db_commit_timeout_ms
    # 仅作用于本事务，连接归还后不改变后续请求的会话设置。
    session.execute(
        text("SELECT set_config('statement_timeout', :value, true)"),
        {"value": str(settings.db_statement_timeout_ms)},
    )
    session.execute(text("SELECT set_config('lock_timeout', '3000', true)"))
    if settings.db_require_sync_apply:
        policy = session.execute(
            text(
                "SELECT current_setting('synchronous_commit'), "
                "current_setting('synchronous_standby_names')"
            )
        ).one()
        if policy[0] != "remote_apply" or not policy[1].strip():
            raise HTTPException(503, "synchronous_replication_not_configured")


def commit_write(session: Session) -> None:
    """提交响应丢失或同步等待被取消时，保留不确定状态，调用方按幂等键核对。"""
    connection = session.connection()
    driver = connection.connection.driver_connection
    warnings = []
    expired = Event()
    timer = None
    cleaned = False

    def notice(diagnostic):
        if diagnostic.severity_nonlocalized in {"WARNING", "ERROR", "FATAL"}:
            warnings.append(diagnostic.sqlstate)

    supports_notice = hasattr(driver, "add_notice_handler")
    if supports_notice:
        driver.add_notice_handler(notice)

    def cancel_wait():
        expired.set()
        try:
            driver.cancel_safe(timeout=1)
        except psycopg.Error:
            # 无法取消时关闭该连接，避免仍在提交的连接被继续借给其他请求。
            logger.warning("Commit cancellation failed; closing the connection")
            driver.close()

    def cleanup(*_):
        nonlocal cleaned
        if cleaned:
            return
        if timer is not None:
            timer.cancel()
            timer.join()
        if supports_notice:
            driver.remove_notice_handler(notice)
        cleaned = True

    real_postgres = isinstance(driver, psycopg.Connection)
    if real_postgres:
        # after_commit 在连接归还池之前执行，必须等取消操作结束，避免误伤下一位借用者。
        event.listen(session, "after_commit", cleanup)
    try:
        if real_postgres:
            session.flush()
            # PostgreSQL 16 在提交阶段关闭 statement_timeout，提交等待须独立计时。
            timer = Timer(session.info.get("commit_timeout_ms", 10000) / 1000, cancel_wait)
            timer.daemon = True
            timer.start()
        session.commit()
    except DBAPIError as error:
        logger.warning("Commit outcome requires reconciliation (%s)", type(error).__name__)
        cleanup()
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(503, "commit_outcome_unknown") from None
    finally:
        cleanup()
        if real_postgres:
            event.remove(session, "after_commit", cleanup)
    if warnings or expired.is_set():
        # PostgreSQL 取消同步复制等待可能只返回 WARNING，本地事务却已经提交。
        raise HTTPException(503, "commit_outcome_unknown")
