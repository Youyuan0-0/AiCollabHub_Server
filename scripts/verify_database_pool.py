"""只读验证真实 PostgreSQL 的连接限额、空闲回收与自动重连；不创建业务记录。"""

import json
import time

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.core.config import Settings
from app.db.session import create_db_engine


def main() -> None:
    """最多占用一条连接，短超时仅用于本次检查，退出时释放测试连接。"""
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("请先配置 PostgreSQL")
    engine = create_db_engine(
        settings.database_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
        idle_session_timeout=2,
        application_name="aicollabhub:pool-check",
    )
    try:
        with engine.connect() as connection:
            first_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            program = connection.scalar(text("SHOW application_name"))
            assert connection.scalar(text("SHOW idle_session_timeout")) == "2s"
            try:
                with engine.connect():
                    raise AssertionError("连接池超出指定上限")
            except PoolTimeout:
                pass
            # 事务中空闲不应受普通 idle_session_timeout 影响。
            time.sleep(3)
            assert connection.scalar(text("SELECT pg_backend_pid()")) == first_pid
            # 正在执行的查询也不应被普通空闲超时取消。
            connection.execute(text("SELECT pg_sleep(3)"))

        # 归还时回滚只读事务，让服务端进入普通 idle 状态并回收连接。
        time.sleep(3)
        with engine.connect() as connection:
            second_pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert second_pid != first_pid
            assert not connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"),
                {"pid": first_pid},
            )
            assert connection.scalar(text("SHOW application_name")) == program
        # 连续请求应正常复用新连接，不能每次都增加一条连接。
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT pg_backend_pid()")) == second_pid
        print(
            json.dumps(
                {
                    "pool_limit": "passed",
                    "transaction_and_query_preserved": "passed",
                    "idle_connection_released": "passed",
                    "automatic_reconnect_and_reuse": "passed",
                    "application_name": program,
                }
            )
        )
    except Exception as error:
        # SQLAlchemy 异常可能包含连接上下文，仅输出类型和 SQLSTATE。
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "sqlstate": getattr(getattr(error, "orig", error), "sqlstate", None),
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
