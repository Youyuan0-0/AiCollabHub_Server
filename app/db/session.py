from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def create_db_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_timeout: float = 5,
    pool_recycle: int = 1800,
    connect_timeout: int = 5,
    target_role: Literal["primary", "standby"] | None = None,
    readonly: bool = False,
) -> Engine:
    # 从连接池取出连接时检查存活状态，减少长时间闲置后使用失效连接的风险。
    options = {}
    if make_url(database_url).get_backend_name() == "postgresql":
        connect_args = {"connect_timeout": connect_timeout}
        if target_role is not None:
            # libpq 在连接建立时校验目标角色，避免端点切换后连错主备。
            connect_args["target_session_attrs"] = (
                "standby" if target_role == "standby" else "primary" if readonly else "read-write"
            )
        options.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
            connect_args=connect_args,
        )
        if readonly:
            options["execution_options"] = {"postgresql_readonly": True}
    return create_engine(database_url, pool_pre_ping=True, **options)


def get_engine(request: Request) -> Engine:
    # 引擎由应用生命周期统一管理，请求仅获取引用，不重复创建连接池。
    engine: Engine | None = request.app.state.database_engine
    if engine is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
        )
    return engine


def get_session(engine: Annotated[Engine, Depends(get_engine)]) -> Iterator[Session]:
    # 每个请求使用独立会话；业务服务显式 commit，未提交内容在会话关闭时回滚。
    # 查询前不自动 flush；提交后保留已加载属性，便于构造响应。
    with Session(engine, autoflush=False, expire_on_commit=False) as session:
        yield session


# 在同步路由中声明此类型，由 FastAPI 注入并回收请求级数据库会话。
SessionDependency = Annotated[Session, Depends(get_session)]
