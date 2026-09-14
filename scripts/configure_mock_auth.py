"""生成被 Git 忽略的本地认证配置；不输出密钥，不修改已有服务凭证。"""

import argparse
import secrets
from pathlib import Path

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-port", type=int, default=18880)
    parser.add_argument("--iam-port", type=int, default=18002)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / ".runtime" / "mock-auth.env"
    previous = dotenv_values(path) if path.exists() else {}
    secret = previous.get("AUTH_MOCK_SECRET") or secrets.token_urlsafe(48)
    values = {
        "AUTH_MODE": "mock",
        "AUTH_PUBLIC_ORIGIN": f"http://127.0.0.1:{args.gateway_port}",
        "AUTH_MOCK_IAM_URL": f"http://127.0.0.1:{args.iam_port}",
        "AUTH_MOCK_SECRET": secret,
        "AUTH_COOKIE_SECURE": "false",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    print("Local authentication configuration: .runtime/mock-auth.env")


if __name__ == "__main__":
    main()
