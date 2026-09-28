from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings

logger = logging.getLogger(__name__)


def prepare_write(session: Session, settings: Settings) -> None:
    """为 PostgreSQL 当前写事务设置语句和锁等待超时。"""
    if session.bind.dialect.name != "postgresql":
        return
    # 只限制当前事务的语句和锁等待，不改变连接归还后的设置。
    session.execute(
        text(
            "SELECT set_config('statement_timeout', :value, true), "
            "set_config('lock_timeout', '3000', true)"
        ),
        {"value": str(settings.db_statement_timeout_ms)},
    )


def commit_write(session: Session) -> None:
    """提交当前业务事务，将提交阶段的数据库错误标记为结果待核对。

    flush 先暴露可确定的约束错误；进入 commit 后若连接异常，服务端可能已经落盘，
    客户端却未收到确认。此时返回 commit_outcome_unknown，调用方应使用原幂等键
    查询结果，不能生成新请求或盲目重放。后续 rollback 只清理本地事务状态，
    不能证明数据库端此前的提交没有成功。
    """
    # 约束冲突在提交之前交给调用方处理，不能误报为提交结果不确定。
    session.flush()
    try:
        session.commit()
    except DBAPIError as error:
        logger.warning("Commit outcome requires reconciliation (%s)", type(error).__name__)
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise HTTPException(503, "commit_outcome_unknown") from None
