"""管理员日志查询入口；不提供修改、删除或清空接口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.db.session import SessionDependency
from app.modules.admin.logs import service
from app.modules.admin.logs.schemas import (
    LogOptions,
    LogPage,
    OperationLogItem,
    OperationLogQuery,
    SystemLogDetail,
    SystemLogItem,
    SystemLogQuery,
)
from app.modules.common.auth.service import authenticated_session


def require_log_admin(request: Request, db: SessionDependency) -> None:
    """每次核验有效管理员会话；匿名演示模式也不能读取真实日志。"""
    if request.app.state.settings.auth_mode == "disabled":
        raise HTTPException(403, "查看日志需要启用登录认证")
    authenticated_session(db, request, "admin")


router = APIRouter(
    prefix="/admin/logs", tags=["admin logs"], dependencies=[Depends(require_log_admin)]
)


@router.get("/options")
def log_options() -> LogOptions:
    """读取筛选项和中文标签。"""
    return service.options()


@router.get("/operations")
def operation_logs(
    db: SessionDependency, query: Annotated[OperationLogQuery, Query()]
) -> LogPage[OperationLogItem]:
    """查询成功操作的筛选分页。"""
    return service.operation_page(db, query)


@router.get("/system")
def system_logs(
    db: SessionDependency, query: Annotated[SystemLogQuery, Query()]
) -> LogPage[SystemLogItem]:
    """查询系统事件的筛选分页。"""
    return service.system_page(db, query)


@router.get("/system/{record_id}")
def system_log(record_id: UUID, db: SessionDependency) -> SystemLogDetail:
    """查看单条系统日志诊断信息。"""
    return service.system_detail(db, record_id)
