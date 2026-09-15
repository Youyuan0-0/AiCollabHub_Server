"""在项目 .env 中初始化模拟登录；保留服务凭证和已生成的联调密钥。"""

import argparse
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key


def configure_mock_auth(
    path: Path, *, gateway_port: int | None = None, iam_port: int | None = None
) -> None:
    previous = dotenv_values(path) if path.exists() else {}
    if previous.get("APP_ENV") == "production":
        raise ValueError("Mock IAM is prohibited in production")
    values = {
        "AUTH_MODE": "mock",
        "AUTH_PUBLIC_ORIGIN": (
            f"http://127.0.0.1:{gateway_port}"
            if gateway_port is not None
            else previous.get("AUTH_PUBLIC_ORIGIN") or "http://127.0.0.1:18880"
        ),
        "AUTH_MOCK_IAM_URL": (
            f"http://127.0.0.1:{iam_port}"
            if iam_port is not None
            else previous.get("AUTH_MOCK_IAM_URL") or "http://127.0.0.1:18002"
        ),
        "AUTH_MOCK_SECRET": previous.get("AUTH_MOCK_SECRET") or secrets.token_urlsafe(48),
        "AUTH_COOKIE_SECURE": previous.get("AUTH_COOKIE_SECURE") or "false",
    }
    if gateway_port is not None:
        values["AUTH_COOKIE_SECURE"] = "false"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 仅更新认证字段；重复执行不会重置密钥，也不会覆盖数据库、Redis、MinIO 配置。
    for key, value in values.items():
        if previous.get(key) != value:
            set_key(path, key, value)


def main():
    parser = argparse.ArgumentParser(
        description="Initialize mock authentication in the project .env"
    )
    parser.add_argument("--gateway-port", type=int, help="Override the local gateway port")
    parser.add_argument("--iam-port", type=int, help="Override the local mock IAM port")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for port in (args.gateway_port, args.iam_port):
        if port is not None and not 1 <= port <= 65535:
            parser.error("Ports must be between 1 and 65535")
    try:
        configure_mock_auth(root / ".env", gateway_port=args.gateway_port, iam_port=args.iam_port)
    except ValueError as error:
        parser.error(str(error))
    print("Mock authentication configured in .env. Existing service settings were preserved.")


if __name__ == "__main__":
    main()
