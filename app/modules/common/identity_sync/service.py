import hashlib
import json
import logging

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import (
    ActorType,
    AuthSession,
    OperationAction,
    OperationTarget,
    SessionRecordType,
    User,
    UserRole,
    UserStatus,
    utc_now,
)
from app.modules.common.auth.service import write_operation
from app.modules.common.identity_sync.access import insert_missing, lock_identity
from app.modules.common.identity_sync.models import IamSyncReceipt
from app.modules.common.identity_sync.schemas import RoleChange, SyncBatch, SyncResult, UserChange

logger = logging.getLogger(__name__)


def result_for(receipt: IamSyncReceipt, *, is_duplicate: bool = False) -> SyncResult:
    """回执证明事务已提交，重复推送复用编号但不重复更新。"""
    return SyncResult(
        receipt_id=receipt.id,
        resource_type=receipt.resource_type,
        sync_time=receipt.sync_time,
        duplicate=is_duplicate,
    )


def payload_hash(batch: SyncBatch) -> str:
    """规范化已校验业务字段，数组保留原顺序，不包含传输认证或请求编号。"""
    canonical = json.dumps(
        batch.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def compare_version(previous: int | None, incoming: int, is_same_value: bool) -> str:
    """拒绝同时间戳内容冲突，忽略更旧推送，不使用本地到达顺序。"""
    if previous is None or incoming > previous:
        return "applied"
    if incoming < previous:
        return "ignored"
    if not is_same_value:
        raise HTTPException(409, "sync_version_conflict")
    return "unchanged"


def apply_user(db: Session, user: User, change: UserChange, batch: SyncBatch) -> tuple[bool, int]:
    """更新资料和状态；较新同步时间始终保存，实际信息变化才刷新更新时间。"""
    status = UserStatus.DISABLED if batch.action == "recycle" else UserStatus(change.status)
    display_name = change.display_name if change.display_name is not None else user.display_name
    is_same = user.status == status and user.display_name == display_name
    outcome = compare_version(user.user_sync_time, batch.sync_time, is_same)
    revoked = 0
    if outcome == "applied":
        user.user_sync_time = batch.sync_time
        if not is_same:
            user.status, user.display_name, user.updated_at = status, display_name, utc_now()
        if status != UserStatus.ACTIVE:
            revoked = len(
                db.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.user_id == user.id,
                        AuthSession.record_type == SessionRecordType.SESSION,
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=utc_now())
                    .returning(AuthSession.id)
                ).all()
            )
    return outcome == "applied" and not is_same, revoked


def apply_role(user: User, change: RoleChange, batch: SyncBatch) -> bool:
    """更新指定管理员授权，角色推送不得激活待确认或停用用户。"""
    role = (
        UserRole.ADMIN
        if batch.action == "push" and change.status == "authorized"
        else UserRole.USER
    )
    is_same = user.role == role
    outcome = compare_version(user.role_sync_time, batch.sync_time, is_same)
    if outcome == "applied":
        user.role_sync_time = batch.sync_time
        if not is_same:
            user.role, user.updated_at = role, utc_now()
    return outcome == "applied" and not is_same


def receive(
    db: Session, settings: Settings, batch: SyncBatch, *, request_id: str | None = None
) -> SyncResult:
    """通过摘要唯一键协调并发重试，用户、会话、成功日志与回执同事务提交。"""
    try:
        prepare_write(db, settings)
        fingerprint = payload_hash(batch)
        inserted = insert_missing(
            db,
            IamSyncReceipt,
            dict(
                payload_hash=fingerprint,
                resource_type=batch.resource_type,
                action=batch.action,
                sync_time=batch.sync_time,
            ),
            conflict_columns=["payload_hash"],
        )
        receipt = db.scalars(
            select(IamSyncReceipt).where(IamSyncReceipt.payload_hash == fingerprint)
        ).one()
        if inserted:
            changed, revoked = 0, 0
            for change in sorted(batch.data, key=lambda item: item.subject):
                user = lock_identity(db, change.subject)
                if isinstance(change, UserChange):
                    updated, count = apply_user(db, user, change, batch)
                    revoked += count
                else:
                    updated = apply_role(user, change, batch)
                changed += int(updated)
            if changed or revoked:
                write_operation(
                    db,
                    action=OperationAction.SYNC,
                    actor_type=ActorType.IAM,
                    target_type=OperationTarget.IDENTITY_SYNC,
                    target_id=receipt.id,
                    request_id=request_id,
                    data={"changedUsers": changed, "revokedSessions": revoked},
                )
        result = result_for(receipt, is_duplicate=not inserted)
        commit_write(db)
        logger.info(
            "identity_sync result=%s request_id=%s receipt_id=%s",
            "applied" if inserted else "duplicate",
            request_id,
            receipt.id,
            extra={
                "event": "identity_sync_applied" if inserted else "identity_sync_duplicate",
                "request_id": request_id,
                "details": {"receiptId": receipt.id, "resourceType": batch.resource_type},
            },
        )
        return result
    except (HTTPException, SQLAlchemyError) as error:
        db.rollback()
        logger.warning(
            "identity_sync failed=%s request_id=%s",
            type(error).__name__,
            request_id,
            extra={
                "event": "identity_sync_failed",
                "request_id": request_id,
                "details": {"errorType": type(error).__name__, "resourceType": batch.resource_type},
            },
        )
        raise
