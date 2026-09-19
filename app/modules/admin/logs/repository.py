"""日志只读仓储；筛选和分页均在数据库执行。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, String, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session, defer
from sqlalchemy.sql.elements import ColumnElement

from app.modules.admin.logs.presentation import summary_expression
from app.modules.admin.logs.schemas import (
    ACTION_LABELS,
    TARGET_LABELS,
    LogQuery,
    OperationLogQuery,
    SystemLogQuery,
)
from app.modules.common.auth.models import ActorType, OperationLog, User, UserRole
from app.modules.common.system_logs import SystemLog


def operation_summary() -> ColumnElement[str]:
    """从已确认动作生成摘要，同一表达式用于列表展示和关键词查询。"""
    action_label = case(ACTION_LABELS, value=cast(OperationLog.action, String), else_="业务操作")
    target_label = case(TARGET_LABELS, value=cast(OperationLog.target_type, String), else_="对象")
    scope = OperationLog.data["scope"].as_string()
    return case(
        (
            OperationLog.action == "login",
            case(
                (scope == "admin", "登录管理端"), (scope == "user", "登录用户端"), else_="登录平台"
            ),
        ),
        (OperationLog.action == "logout", "退出登录"),
        (OperationLog.action == "single_logout", "撤销对应票据的平台会话"),
        (OperationLog.action == "sync", "同步用户身份与权限"),
        else_=action_label + literal(" · ") + target_label,
    )


def operation_actor_name() -> ColumnElement[str]:
    """非用户操作明确显示 IAM 或平台系统，用户缺少姓名时回退稳定身份编号。"""
    return case(
        (OperationLog.actor_type == "iam", "集团 IAM"),
        (OperationLog.actor_type == "system", "平台系统"),
        else_=func.coalesce(
            User.display_name, User.iam_subject, cast(OperationLog.user_id, String)
        ),
    )


def _common_conditions(
    model: type[OperationLog] | type[SystemLog], query: LogQuery, request_id: UUID | None = None
) -> list[ColumnElement[bool]]:
    """普通查询默认最近 24 小时；只有系统日志可按精确请求编号跨时间查询。"""
    conditions = []
    start_at, end_at = query.start_at, query.end_at
    if start_at is None and request_id is None:
        end_at = datetime.now(UTC)
        start_at = end_at - timedelta(days=1)
    if start_at is not None:
        conditions.extend([model.created_at >= start_at, model.created_at <= end_at])
    if request_id is not None:
        conditions.append(model.request_id == request_id)
    return conditions


def _operation_select() -> Select:
    return select(
        OperationLog,
        operation_actor_name().label("actor_name"),
        operation_summary().label("summary"),
    ).outerjoin(User, OperationLog.user_id == User.id)


def operation_page(
    db: Session, query: OperationLogQuery
) -> tuple[list[tuple[OperationLog, str, str]], int]:
    """在受限时间段查询操作，列表不取回业务 JSON。"""
    conditions = _common_conditions(OperationLog, query)
    for column, value in (
        (OperationLog.action, query.action),
        (OperationLog.target_type, query.target_type),
    ):
        if value is not None:
            conditions.append(column == value)
    if query.operator == "iam":
        conditions.append(OperationLog.actor_type == ActorType.IAM)
    elif query.operator == "admin":
        # actor_type=user 也包含普通用户，管理员筛选必须核对真实角色。
        conditions.extend([OperationLog.actor_type == ActorType.USER, User.role == UserRole.ADMIN])
    if query.q:
        conditions.append(
            or_(
                operation_actor_name().icontains(query.q, autoescape=True),
                User.iam_subject.icontains(query.q, autoescape=True),
                OperationLog.target_id.icontains(query.q, autoescape=True),
                operation_summary().icontains(query.q, autoescape=True),
            )
        )
    total = (
        db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .outerjoin(User, OperationLog.user_id == User.id)
            .where(*conditions)
        )
        or 0
    )
    statement = (
        _operation_select()
        .options(defer(OperationLog.data))
        .where(*conditions)
        .order_by(
            *(
                column.asc() if query.sort_order == "asc" else column.desc()
                for column in (OperationLog.created_at, OperationLog.id)
            )
        )
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return [(row[0], row[1], row[2]) for row in db.execute(statement)], total


def system_page(db: Session, query: SystemLogQuery) -> tuple[list[tuple[SystemLog, str]], int]:
    """先对完整结果集稳定排序再分页，列表不传输堆栈和结构化上下文。"""
    conditions = _common_conditions(SystemLog, query, query.request_id)
    if query.level is not None:
        conditions.append(SystemLog.level == query.level)
    if query.source is not None:
        conditions.append(SystemLog.source == query.source)
    if query.q:
        conditions.append(
            or_(
                SystemLog.message.icontains(query.q, autoescape=True),
                SystemLog.event.icontains(query.q, autoescape=True),
                summary_expression().icontains(query.q, autoescape=True),
            )
        )
    total = db.scalar(select(func.count()).select_from(SystemLog).where(*conditions)) or 0
    records = db.execute(
        select(SystemLog, summary_expression())
        .options(defer(SystemLog.details))
        .where(*conditions)
        .order_by(
            *(
                column.asc() if query.sort_order == "asc" else column.desc()
                for column in (SystemLog.created_at, SystemLog.id)
            )
        )
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    ).all()
    return [(row[0], row[1]) for row in records], total


def system_detail(db: Session, record_id: UUID) -> tuple[SystemLog, str] | None:
    """按主键读取单条完整系统日志。"""
    row = db.execute(
        select(SystemLog, summary_expression()).where(SystemLog.id == record_id)
    ).first()
    return (row[0], row[1]) if row is not None else None
