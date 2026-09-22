"""在显式开发库的隔离 schema 中验证新会话仓储，不修改 public 或执行旧迁移。"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings


def main() -> None:
    """读取指定配置，只向测试子进程传递连接；报告不打印服务凭证。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/conversations-postgresql.xml")
    )
    args = parser.parse_args()
    settings = Settings(_env_file=args.env)
    if settings.app_env not in {"development", "test"} or settings.database_url is None:
        parser.error("Requires an explicitly configured development/test PostgreSQL database")
    secret = settings.database_url.get_secret_value()
    if make_url(secret).get_backend_name() != "postgresql":
        parser.error("Requires PostgreSQL")
    report = args.output.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, "RESOURCE_FOUNDATION_TEST_DATABASE_URL": secret}
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_foundation_home_feedback.py",
            "-k",
            "chat",
            "-q",
            "--tb=short",
            f"--junitxml={report}",
        ],
        cwd=root,
        env=environment,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
