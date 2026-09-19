"""日志只读业务与输出脱敏。"""

import json
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import JsonValue
from sqlalchemy.orm import Session

from app.core.log_sanitizer import MAX_DETAILS_LENGTH, redact_text, sanitize_value
from app.modules.admin.logs import repository
from app.modules.admin.logs.schemas import (
    ACTION_LABELS,
    LEVEL_LABELS,
    SOURCE_LABELS,
    TARGET_LABELS,
    LogOption,
    LogOptions,
    LogPage,
    OperationLogItem,
    OperationLogQuery,
    SystemLogDetail,
    SystemLogItem,
    SystemLogQuery,
)
from app.modules.common.auth.models import OperationAction, OperationLog, OperationTarget
from app.modules.common.system_logs import SystemLog


def options() -> LogOptions:
    """动作和对象选项以持久化模型枚举为准，标签仅负责中文展示。"""
    return LogOptions(
        actions=[
            LogOption(value=item.value, label=ACTION_LABELS.get(item.value, item.value))
            for item in OperationAction
        ],
        target_types=[
            LogOption(value=item.value, label=TARGET_LABELS.get(item.value, item.value))
            for item in OperationTarget
        ],
        operators=[
            LogOption(value="admin", label="管理员"),
            LogOption(value="iam", label="集团 IAM"),
        ],
        **{
            key: [LogOption(value=value, label=label) for value, label in values.items()]
            for key, values in {
                "levels": LEVEL_LABELS,
                "sources": SOURCE_LABELS,
            }.items()
        },
    )


def safe_context(value: dict) -> dict[str, JsonValue]:
    """读取时再次脱敏，避免历史记录绕过当前采集器的脱敏规则。"""
    cleaned = sanitize_value(value)
    if not isinstance(cleaned, dict):
        return {}
    if len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) > MAX_DETAILS_LENGTH:
        return {"truncated": True}
    return cast(dict[str, JsonValue], cleaned)


def operation_item(record: OperationLog, actor_name: str, summary: str) -> OperationLogItem:
    """整理列表字段，用户名称为当前身份资料，原始用户 UUID 始终保留。"""
    return OperationLogItem(
        id=record.id,
        actor_type=record.actor_type,
        actor_name=redact_text(actor_name),
        user_id=record.user_id,
        action=record.action,
        target_type=record.target_type,
        target_id=redact_text(record.target_id) if record.target_id else None,
        summary=summary,
        request_id=record.request_id,
        created_at=record.created_at,
    )


def system_item(record: SystemLog, summary: str) -> SystemLogItem:
    """摘要再次脱敏，并保留原始事件编码用于定位。"""
    return SystemLogItem(
        id=record.id,
        level=record.level,
        source=redact_text(record.source),
        event=redact_text(record.event),
        message=redact_text(record.message),
        summary=summary,
        request_id=record.request_id,
        created_at=record.created_at,
    )


def operation_page(db: Session, query: OperationLogQuery) -> LogPage[OperationLogItem]:
    """只读查询操作日志，返回数据库总数与当前页。"""
    rows, total = repository.operation_page(db, query)
    return LogPage(
        items=[operation_item(*row) for row in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


def system_page(db: Session, query: SystemLogQuery) -> LogPage[SystemLogItem]:
    """只读查询系统日志，诊断上下文仅在详情返回。"""
    rows, total = repository.system_page(db, query)
    return LogPage(
        items=[system_item(*row) for row in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


def system_detail(db: Session, record_id: UUID) -> SystemLogDetail:
    """读取单条技术事件，额外清理历史 JSON 中的敏感字段。"""
    row = repository.system_detail(db, record_id)
    if row is None:
        raise HTTPException(404, "系统日志不存在")
    record, summary = row
    details = safe_context(record.details)
    return SystemLogDetail(
        **system_item(record, summary).model_dump(),
        details=details,
    )
