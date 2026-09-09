from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


def create_db_engine(database_url: str) -> Engine:
    # 从连接池取出连接时检查存活状态，减少长时间闲置后使用失效连接的风险。
    return create_engine(database_url, pool_pre_ping=True)


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
