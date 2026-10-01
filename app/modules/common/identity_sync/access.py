from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.base import Base
from app.modules.common.auth.models import User, UserRole, UserStatus


def insert_missing(
    db: Session, model: type[Base], values: dict, *, conflict_columns: list[str]
) -> bool:
    """通过唯一键与 RETURNING 协调并发插入，不使用先查后插。"""
    insert = {"postgresql": postgres_insert, "sqlite": sqlite_insert}[db.get_bind().dialect.name]
    statement = (
        insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    )
    return (
        db.execute(statement.returning(next(iter(model.__table__.primary_key)))).first() is not None
    )


def lock_identity(db: Session, subject: str) -> User:
    """创建待确认用户并锁定同一行，串行处理登录、资料及授权同步。"""
    insert_missing(db, User, {"iam_subject": subject}, conflict_columns=["iam_subject"])
    return db.scalars(
        select(User)
        .where(User.iam_subject == subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def can_access_admin(user: User) -> bool:
    """以本地用户状态和角色校验权限，不从演示名单实时回退授权。"""
    return user.status == UserStatus.ACTIVE and user.role == UserRole.ADMIN
