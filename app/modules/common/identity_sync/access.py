import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.modules.common.identity_sync.models import IdentityRole, IdentityState

ADMIN_ROLE = "platform_admin"


def insert_missing(db: Session, model: type[Base], values: dict) -> bool:
    """按主键原子插入缺失记录，返回本次是否新增，兼容 PostgreSQL 和 SQLite。"""
    # 两种当前支持的数据库都使用原子 UPSERT，不以“先查后插”处理并发。
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[db.get_bind().dialect.name]
    keys = [column.name for column in model.__table__.primary_key]
    statement = insert(model).values(**values).on_conflict_do_nothing(index_elements=keys)
    # psycopg 的 INSERT rowcount 可能为 -1，以 RETURNING 判断本次是否真正插入。
    return (
        db.execute(statement.returning(next(iter(model.__table__.primary_key)))).first() is not None
    )


def lock_identity(db: Session, subject: str) -> IdentityState:
    """创建并锁定身份状态行，串行处理该身份的登录回调和同步事件。"""
    # 登录回调与同步推送锁定同一身份行，防止停用完成后迟到的回调再建立有效会话。
    # 并发首次插入交给唯一约束协调；各批次按 subject 排序加锁，减少锁顺序冲突。
    insert_missing(
        db,
        IdentityState,
        dict(subject=subject, status="active", sync_time=0, updated_at=int(time.time())),
    )
    return db.scalar(
        select(IdentityState)
        .where(IdentityState.subject == subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def is_identity_enabled(db: Session, subject: str) -> bool:
    """检查身份是否有效；尚未收到状态推送的模拟身份保持可用。"""
    value = db.get(IdentityState, subject)
    return value is None or value.status == "active"


def can_access_admin(db: Session, settings: Settings, subject: str | None) -> bool:
    """优先使用同步后的授权记录判断管理员准入，撤权后不回退到模拟名单。"""
    if not subject or settings.auth_mode != "mock" or not is_identity_enabled(db, subject):
        return False
    grant = db.get(IdentityRole, (subject, ADMIN_ROLE))
    # 同步过的授权/撤权记录始终优先，不能回退到模拟名单重新获得管理员权限。
    if grant is not None:
        return grant.authorized
    return subject in settings.auth_mock_admin_subjects
