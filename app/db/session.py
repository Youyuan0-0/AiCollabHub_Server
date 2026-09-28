import os
import re
import socket
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session


def normalize_database_url(database_url: str) -> URL:
    """常见 PostgreSQL URL 统一使用已安装的 Psycopg 3，保留密码转义及查询参数。"""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def create_db_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 2,
    pool_timeout: float = 5,
    pool_recycle: int = 1800,
    connect_timeout: int = 5,
    idle_session_timeout: int = 120,
    application_name: str = "aicollabhub",
) -> Engine:
    """创建当前 worker 使用的有界连接池，不在此执行业务查询。

    pool_size 为常驻连接上限，max_overflow 为临时溢出额度；部署总额度需要再乘
    worker 数量。pool_timeout 是借连接的等待秒数，connect_timeout 是建立连接的
    等待秒数，pool_recycle 是池内连接的回收年龄，三者不是同一个超时。

    PostgreSQL 的 idle_session_timeout 按秒限制普通空闲连接，0 表示关闭，
    只影响本引擎新建的连接。pre_ping 可替换失效连接，但不会重试已执行的业务事务。
    """
    # 从连接池取出连接时检查存活状态，减少长时间闲置后使用失效连接的风险。
    options = {}
    url = normalize_database_url(database_url)
    if url.get_backend_name() == "postgresql":
        # PostgreSQL 程序名最多 63 字节；保留主机和 PID，便于区分同一出口下的实例。
        host = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())[:20]
        suffix = f":{host}:{os.getpid()}"
        prefix = re.sub(r"[^A-Za-z0-9_:.-]", "_", application_name)
        connect_args = {
            "connect_timeout": connect_timeout,
            "target_session_attrs": "read-write",
            "application_name": prefix[: 63 - len(suffix)] + suffix,
            # 普通空闲由服务端回收，pre_ping 在下次借用时重连；不改变事务空闲超时。
            # local 仍等待本机 WAL 落盘；连接级设置避免继承旧环境的远端确认策略。
            "options": (
                str(url.query.get("options", ""))
                + f" -c idle_session_timeout={idle_session_timeout}s"
                + " -c synchronous_commit=local"
            ).strip(),
        }
        options.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            # 优先复用最近归还的连接，让较少使用的连接有机会达到空闲回收时限。
            pool_use_lifo=True,
            connect_args=connect_args,
        )
    return create_engine(url, pool_pre_ping=True, **options)


def get_engine(request: Request) -> Engine:
    """取得应用共用的数据库引擎，未配置时返回 503，不重复创建连接池。"""
    engine: Engine | None = request.app.state.database_engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
        )
    return engine


def get_session(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Session]:
    """提供请求独享的会话；业务显式提交，未提交内容随会话关闭回滚。"""
    # 查询前不自动 flush；提交后保留已加载属性，便于构造响应。
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        yield session


# 在同步路由中声明此类型，由 FastAPI 注入并回收请求级数据库会话。
SessionDependency = Annotated[Session, Depends(get_session)]
