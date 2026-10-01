"""仅供服务器运维命令使用的本地角色管理。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import User, UserRole, UserStatus, utc_now
from app.modules.common.system_logs import SystemLog, SystemLogLevel


def set_local_role(
    db: Session,
    settings: Settings,
    *,
    work_no: str,
    role: UserRole,
    operator: str,
    host: str,
) -> bool:
    """修改已存在账号的角色并原子记录运维日志；重复设置返回 False。"""
    if settings.auth_identity_sync_enabled:
        raise ValueError("身份同步已启用，请通过 IAM 授权，不能同时使用本地角色命令。")
    if not work_no or work_no != work_no.strip() or len(work_no) > 200:
        raise ValueError("workNo 必须是完整的集团用户编号，不能包含首尾空格。")
    role = UserRole(role)
    try:
        prepare_write(db, settings)
        user = db.scalar(select(User).where(User.iam_subject == work_no).with_for_update())
        if user is None:
            raise ValueError("用户不存在，请先使用该账号通过 IAM 登录平台一次。")
        if role == UserRole.ADMIN and user.status != UserStatus.ACTIVE:
            raise ValueError("只能向正常启用的用户授予管理员权限。")
        if user.role == role:
            db.rollback()
            return False
        previous_role = user.role
        user.role = role
        user.updated_at = utc_now()
        # 运维账号不是平台用户，不能把被授权用户冒充操作人。
        db.add(
            SystemLog(
                level=SystemLogLevel.INFO,
                source="auth",
                event="auth_local_role_changed",
                message="运维命令修改平台用户角色",
                details={
                    "operator": operator,
                    "host": host,
                    "userId": str(user.id),
                    "workNo": work_no,
                    "previousRole": previous_role.value,
                    "role": role.value,
                },
            )
        )
        commit_write(db)
        return True
    except Exception:
        db.rollback()
        raise
