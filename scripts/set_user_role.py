"""在仅单点登录模式下，按真实 workNo 授予或撤销本平台管理员权限。"""

import argparse
import getpass
import socket

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import create_db_engine
from app.modules.common.auth.local_roles import set_local_role
from app.modules.common.auth.models import UserRole


def main() -> None:
    """从后端目录读取环境配置，仅修改已经登录建号的用户。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-no", required=True, help="CAS 验票返回的真实 workNo")
    parser.add_argument("--role", required=True, choices=[role.value for role in UserRole])
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("请配置 POSTGRES_* 或 DATABASE_URL。")
    engine = create_db_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    try:
        with Session(engine) as db:
            changed = set_local_role(
                db,
                settings,
                work_no=args.work_no,
                role=UserRole(args.role),
                operator=getpass.getuser(),
                host=socket.gethostname(),
            )
        result = "已更新" if changed else "无需修改"
        print(f"{result}：workNo={args.work_no}，role={args.role}")
    except ValueError as error:
        parser.error(str(error))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
