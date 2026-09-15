"""定期清理过期登录状态；审计记录按配置的保留周期清理。"""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import create_db_engine
from app.db.transactions import commit_write, prepare_write
from app.modules.common.auth.models import AuthEventLog, AuthLoginRequest, AuthSession
from app.modules.common.auth.service import now


def main():
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required")
    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        with Session(engine) as db:
            prepare_write(db, settings)
            count = {}
            # 留出登录等待窗口，避免清掉仍用于拦截迟到回调的登出标记。
            for model, condition in (
                (AuthLoginRequest, AuthLoginRequest.expires_at < now() - settings.auth_login_ttl),
                (AuthSession, AuthSession.expires_at < now() - settings.auth_login_ttl),
                (
                    AuthEventLog,
                    AuthEventLog.created_at < now() - settings.auth_audit_retention_days * 86400,
                ),
            ):
                count[model.__tablename__] = db.execute(delete(model).where(condition)).rowcount
            commit_write(db)
            print(count)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
