"""验票后建立最小本地身份，不从 CAS 报文接收平台管理权限。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.common.auth.cas import Principal
from app.modules.common.auth.models import User, UserRole, UserStatus, utc_now
from app.modules.common.identity_sync.access import insert_missing


def provision_login_user(db: Session, principal: Principal, settings: Settings) -> User:
    """按集团编号首次建号；后续登录保留本地角色和停用状态，由调用方提交。"""
    can_create_active = not settings.auth_identity_sync_enabled or settings.auth_mode == "mock"
    is_mock_admin = (
        settings.auth_mode == "mock" and principal.subject in settings.auth_mock_admin_subjects
    )
    # 唯一约束协调并发首登；初始角色仅用于插入，不能覆盖运维撤销的权限。
    insert_missing(
        db,
        User,
        {
            "iam_subject": principal.subject,
            "display_name": principal.display_name,
            "status": UserStatus.ACTIVE if can_create_active else UserStatus.PENDING,
            "role": UserRole.ADMIN if is_mock_admin else UserRole.USER,
        },
        conflict_columns=["iam_subject"],
    )
    user = db.scalars(
        select(User)
        .where(User.iam_subject == principal.subject)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if (
        settings.auth_mode == "mock"
        and settings.auth_identity_sync_enabled
        and user.status == UserStatus.PENDING
        and user.user_sync_time is None
    ):
        user.status = UserStatus.ACTIVE
        user.updated_at = utc_now()
    if not user.display_name and user.user_sync_time is None:
        user.display_name = principal.display_name
        user.updated_at = utc_now()
    return user
