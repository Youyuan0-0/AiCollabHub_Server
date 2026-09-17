"""反馈查询、提交和处理事务，避免多实例重复提交或互相覆盖。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.feedback.models import FeedbackAttachment, FeedbackEvent, FeedbackRecord
from app.modules.feedback.schemas import (
    FEEDBACK_MODULES,
    FEEDBACK_TYPES,
    FeedbackInput,
    FeedbackQuery,
    FeedbackUpdate,
)


@dataclass(frozen=True)
class FeedbackActor:
    """由服务端校验得到的操作身份。"""

    id: str
    name: str


def actor(request: Request, *, admin: bool = False) -> FeedbackActor:
    """复用业务鉴权结果；未启用登录时仅允许开发环境固定身份。"""
    identity = getattr(request.state, "business_identity", None)
    if identity:
        if admin and identity["scope"] != "admin":
            raise HTTPException(403, "没有处理反馈的权限")
        return FeedbackActor(identity["id"], identity["name"][:100])
    settings = request.app.state.settings
    if settings.auth_mode == "disabled" and settings.app_env != "production":
        return (
            FeedbackActor("demo-admin", "演示管理员")
            if admin
            else FeedbackActor("demo-user", "演示用户")
        )
    raise HTTPException(401, "authentication_required")


def utc_now() -> datetime:
    """返回统一的 UTC 业务时间。"""
    return datetime.now(UTC)


def aware(value: datetime) -> datetime:
    """兼容 SQLite 测试返回的不带时区时间。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def fingerprint(value: dict) -> str:
    """对规范化请求计算摘要，拒绝同一请求编号替换内容。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def owned_record(
    db: Session, record_id: str, owner: FeedbackActor, *, admin: bool = False
) -> FeedbackRecord:
    """在授权范围内定位反馈，不泄露其他提交人的记录。"""
    query = select(FeedbackRecord).where(FeedbackRecord.id == record_id)
    if not admin:
        query = query.where(FeedbackRecord.submitter_id == owner.id)
    record = db.scalar(query)
    if record is None:
        raise HTTPException(404, "反馈不存在或无权查看")
    return record


def attachment_value(item: FeedbackAttachment) -> dict:
    """只返回展示资料，不泄露桶地址及内部对象键。"""
    return {
        "id": item.id,
        "name": item.original_name,
        "size": item.byte_size,
        "contentType": item.content_type,
        "state": item.state,
        "securityLevel": item.security_level,
        "expiresAt": aware(item.expires_at).isoformat() if item.expires_at else None,
    }


def record_value(item: FeedbackRecord, latest: FeedbackEvent | None = None) -> dict:
    """统一列表与详情中的原始资料和状态。"""
    return {
        "id": item.id,
        "ownerName": item.submitter_name,
        "type": item.feedback_type,
        "typeLabel": FEEDBACK_TYPES[item.feedback_type],
        "typeOther": item.type_other,
        "module": item.module_code,
        "moduleLabel": FEEDBACK_MODULES[item.module_code],
        "moduleOther": item.module_other,
        "title": item.title,
        "description": item.description,
        "contact": item.contact,
        "status": item.status,
        "version": item.version,
        "createdAt": aware(item.created_at).isoformat(),
        "updatedAt": aware(item.updated_at).isoformat(),
        "reply": latest.content if latest and latest.action != "submitted" else "",
    }


def detail(db: Session, item: FeedbackRecord, before_version: int | None = None) -> dict:
    """分批读取处理历史；当前最新说明不受历史翻页影响。"""
    events_query = select(FeedbackEvent).where(FeedbackEvent.feedback_id == item.id)
    if before_version is not None:
        events_query = events_query.where(FeedbackEvent.resulting_version < before_version)
    events = list(
        db.scalars(events_query.order_by(FeedbackEvent.resulting_version.desc()).limit(21))
    )
    latest = db.scalar(
        select(FeedbackEvent).where(
            FeedbackEvent.feedback_id == item.id, FeedbackEvent.resulting_version == item.version
        )
    )
    attachments = db.scalars(
        select(FeedbackAttachment)
        .where(FeedbackAttachment.feedback_id == item.id, FeedbackAttachment.state == "attached")
        .order_by(FeedbackAttachment.created_at, FeedbackAttachment.id)
    )
    return {
        **record_value(item, latest),
        "attachments": [attachment_value(a) for a in attachments],
        "events": [
            {
                "id": event.id,
                "action": event.action,
                "actorName": event.actor_name,
                "content": event.content,
                "fromStatus": event.from_status,
                "toStatus": event.to_status,
                "version": event.resulting_version,
                "createdAt": aware(event.created_at).isoformat(),
            }
            for event in events[:20]
        ],
        "nextEventVersion": events[19].resulting_version if len(events) > 20 else None,
    }


def page(db: Session, query: FeedbackQuery, owner: FeedbackActor, *, admin: bool = False) -> dict:
    """查询授权范围内的反馈，并一次性取得各记录的最新处理说明。"""
    filters = [] if admin else [FeedbackRecord.submitter_id == owner.id]
    if query.status != "all":
        filters.append(
            FeedbackRecord.status == ("processing" if query.status == "open" else query.status)
        )
    if query.q.strip():
        filters.append(FeedbackRecord.title.contains(query.q.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(FeedbackRecord).where(*filters)) or 0
    rows = db.execute(
        select(FeedbackRecord, FeedbackEvent)
        .outerjoin(
            FeedbackEvent,
            (FeedbackEvent.feedback_id == FeedbackRecord.id)
            & (FeedbackEvent.resulting_version == FeedbackRecord.version),
        )
        .where(*filters)
        .order_by(FeedbackRecord.created_at.desc(), FeedbackRecord.id.desc())
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    ).all()
    return {
        "items": [record_value(item, event) for item, event in rows],
        "total": total,
        "page": query.page,
        "pageSize": query.page_size,
    }


def submission(db: Session, owner: FeedbackActor, request_id: str) -> FeedbackRecord | None:
    """从主库核对原提交编号对应的反馈。"""
    return db.scalar(
        select(FeedbackRecord).where(
            FeedbackRecord.submitter_id == owner.id, FeedbackRecord.client_request_id == request_id
        )
    )


def submit(
    db: Session, settings: Settings, owner: FeedbackActor, payload: FeedbackInput
) -> tuple[dict, bool]:
    """在单个事务内创建反馈、绑定附件并追加提交记录。"""
    request_id = str(payload.client_request_id)
    data = payload.model_dump(mode="json")
    data["attachment_ids"] = sorted(data["attachment_ids"])
    digest = fingerprint(data)
    existing = submission(db, owner, request_id)
    if existing:
        if existing.payload_hash != digest:
            raise HTTPException(409, "同一提交编号的内容发生变化，请核对已提交记录")
        return detail(db, existing), False
    prepare_write(db, settings)
    now = utc_now()
    record = FeedbackRecord(
        id=str(uuid4()),
        submitter_id=owner.id,
        submitter_name=owner.name,
        feedback_type=payload.type,
        type_other=payload.type_other,
        module_code=payload.module,
        module_other=payload.module_other,
        title=payload.title,
        description=payload.description,
        contact=payload.contact,
        status="processing",
        version=1,
        client_request_id=request_id,
        payload_hash=digest,
        created_at=now,
        updated_at=now,
    )
    try:
        db.add(record)
        db.flush()
        # 排序后逐条条件更新，数据库同时负责归属、过期和并发绑定检查。
        for attachment_id in sorted(map(str, payload.attachment_ids)):
            result = db.execute(
                update(FeedbackAttachment)
                .where(
                    FeedbackAttachment.id == attachment_id,
                    FeedbackAttachment.uploader_id == owner.id,
                    FeedbackAttachment.state == "staged",
                    FeedbackAttachment.feedback_id.is_(None),
                    FeedbackAttachment.expires_at > now,
                )
                .values(feedback_id=record.id, state="attached", expires_at=None, updated_at=now)
            )
            if result.rowcount != 1:
                raise HTTPException(409, "附件未上传完成、已过期或不可使用，请重新上传")
        db.add(
            FeedbackEvent(
                id=str(uuid4()),
                feedback_id=record.id,
                actor_id=owner.id,
                actor_name=owner.name,
                action="submitted",
                from_status=None,
                to_status="processing",
                content="",
                resulting_version=1,
                client_request_id=request_id,
                payload_hash=digest,
                created_at=now,
            )
        )
        commit_write(db)
    except IntegrityError:
        db.rollback()
        existing = submission(db, owner, request_id)
        if existing and existing.payload_hash == digest:
            return detail(db, existing), False
        raise HTTPException(409, "提交编号冲突，请核对已提交记录") from None
    except HTTPException:
        db.rollback()
        raise
    return detail(db, record), True


def process(
    db: Session, settings: Settings, owner: FeedbackActor, record_id: str, payload: FeedbackUpdate
) -> dict:
    """版本条件更新与追加处理记录原子提交，重试先核对原操作。"""
    item = owned_record(db, record_id, owner, admin=True)
    request_id = str(payload.client_request_id)
    digest = fingerprint(payload.model_dump(mode="json"))

    def replay() -> FeedbackEvent | None:
        return db.scalar(
            select(FeedbackEvent).where(
                FeedbackEvent.feedback_id == record_id,
                FeedbackEvent.actor_id == owner.id,
                FeedbackEvent.client_request_id == request_id,
            )
        )

    previous = replay()
    if previous:
        if previous.payload_hash != digest:
            raise HTTPException(409, "同一处理编号不能提交不同内容")
        return detail(db, item)
    if item.version != payload.expected_version:
        raise HTTPException(409, "反馈已被其他人更新，请刷新详情后再处理")
    now = utc_now()
    old_status = item.status
    action = (
        "replied"
        if old_status == payload.status
        else ("resolved" if payload.status == "resolved" else "reopened")
    )
    prepare_write(db, settings)
    try:
        result = db.execute(
            update(FeedbackRecord)
            .where(
                FeedbackRecord.id == record_id,
                FeedbackRecord.version == payload.expected_version,
            )
            .values(
                status=payload.status,
                version=payload.expected_version + 1,
                updated_at=now,
                resolved_at=(item.resolved_at or now) if payload.status == "resolved" else None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            previous = replay()
            if previous and previous.payload_hash == digest:
                return detail(db, owned_record(db, record_id, owner, admin=True))
            raise HTTPException(409, "反馈已被其他人更新，请刷新详情后再处理")
        db.add(
            FeedbackEvent(
                id=str(uuid4()),
                feedback_id=record_id,
                actor_id=owner.id,
                actor_name=owner.name,
                action=action,
                from_status=old_status,
                to_status=payload.status,
                content=payload.reply,
                resulting_version=payload.expected_version + 1,
                client_request_id=request_id,
                payload_hash=digest,
                created_at=now,
            )
        )
        commit_write(db)
    except IntegrityError:
        db.rollback()
        previous = replay()
        if previous and previous.payload_hash == digest:
            return detail(db, owned_record(db, record_id, owner, admin=True))
        raise HTTPException(409, "反馈已更新，请刷新详情后重试") from None
    db.refresh(item)
    return detail(db, item)
