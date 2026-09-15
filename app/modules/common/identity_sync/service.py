import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import AuthSession
from app.modules.common.identity_sync.access import insert_missing, lock_identity
from app.modules.common.identity_sync.models import IdentityRole, IdentityState, IdentitySyncReceipt
from app.modules.common.identity_sync.schemas import RoleChange, SyncBatch, SyncResult, UserChange


def result_for(receipt: IdentitySyncReceipt, *, duplicate: bool = False) -> SyncResult:
    return SyncResult(
        event_id=receipt.event_id,
        resource_type=receipt.resource_type,
        sync_time=receipt.sync_time,
        applied=receipt.applied,
        ignored=receipt.ignored,
        unchanged=receipt.unchanged,
        revoked_sessions=receipt.revoked_sessions,
        duplicate=duplicate,
    )


def compare_version(previous: int, incoming: int, same_value: bool) -> str:
    if incoming < previous:
        return "ignored"
    if incoming == previous:
        if not same_value:
            raise HTTPException(409, "sync_version_conflict")
        return "unchanged"
    return "applied"


def apply_user(
    db: Session, identity: IdentityState, change: UserChange, batch: SyncBatch
) -> tuple[str, int]:
    status = "disabled" if batch.action == "recycle" else change.status
    outcome = compare_version(identity.sync_time, batch.sync_time, identity.status == status)
    revoked = 0
    if outcome == "applied":
        identity.status, identity.sync_time = status, batch.sync_time
        identity.updated_at = int(time.time())
        if status != "active":
            revoked = db.execute(
                update(AuthSession)
                .where(
                    AuthSession.subject == identity.subject,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=int(time.time()))
            ).rowcount
    return outcome, revoked


def apply_role(db: Session, identity: IdentityState, change: RoleChange, batch: SyncBatch) -> str:
    grant = db.get(IdentityRole, (identity.subject, change.role_code))
    authorized = batch.action == "push" and change.status == "authorized"
    outcome = compare_version(
        grant.sync_time if grant else 0,
        batch.sync_time,
        grant.authorized == authorized if grant else False,
    )
    if outcome == "applied":
        if grant is None:
            grant = IdentityRole(subject=identity.subject, role_code=change.role_code)
            db.add(grant)
        grant.authorized, grant.sync_time = authorized, batch.sync_time
        grant.updated_at = int(time.time())
    # 撤管理员角色只收回管理端权限，普通用户端的有效会话仍可使用。
    return outcome


def receive(db: Session, settings: Settings, batch: SyncBatch) -> SyncResult:
    canonical = json.dumps(
        batch.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    current = int(time.time())
    prepare_write(db, settings)
    values = dict(
        event_id=batch.event_id,
        payload_hash=fingerprint,
        resource_type=batch.resource_type,
        sync_time=batch.sync_time,
        received_at=current,
        last_received_at=current,
        applied=0,
        ignored=0,
        unchanged=0,
        revoked_sessions=0,
        attempts=1,
    )
    if not insert_missing(db, IdentitySyncReceipt, values):
        existing = db.scalar(
            select(IdentitySyncReceipt)
            .where(IdentitySyncReceipt.event_id == batch.event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is None or existing.payload_hash != fingerprint:
            raise HTTPException(409, "sync_event_conflict")
        # 重试更新回执次数后提交，核对上次结果不确定的事件，不重复应用身份变化。
        existing.attempts += 1
        existing.last_received_at = current
        result = result_for(existing, duplicate=True)
        commit_write(db)
        return result

    receipt = db.get(IdentitySyncReceipt, batch.event_id)
    for change in sorted(batch.data, key=lambda item: item.subject):
        identity = lock_identity(db, change.subject)
        if isinstance(change, UserChange):
            outcome, revoked = apply_user(db, identity, change, batch)
            receipt.revoked_sessions += revoked
        else:
            outcome = apply_role(db, identity, change, batch)
        setattr(receipt, outcome, getattr(receipt, outcome) + 1)
    result = result_for(receipt)
    # 状态、权限、会话撤销及回执在一个事务内提交；批次冲突时整体回滚。
    commit_write(db)
    return result
