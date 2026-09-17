"""清理过期会话；Redis 登录上下文依靠 TTL，业务操作日志不随认证清理删除。"""

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import create_db_engine
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import AuthSession
from app.modules.common.auth.service import now


def main() -> None:
    """清理超过在途保护窗口的过期会话，不删除用户或操作历史。"""
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required")
    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        with Session(engine) as db:
            prepare_write(db, settings)
            # 留出登录等待窗口，避免清掉仍用于拦截迟到回调的登出标记。
            count = db.execute(
                delete(AuthSession).where(
                    AuthSession.expires_at < now() - timedelta(seconds=settings.auth_login_ttl)
                )
            ).rowcount
            commit_write(db)
            print({AuthSession.__tablename__: count})
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
