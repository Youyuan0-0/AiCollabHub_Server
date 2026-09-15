"""发送最小模拟身份事件；请求先留档，失败后使用原文件重试同一事件。"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.common.identity_sync.schemas import SyncBatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject")
    parser.add_argument("--resource", choices=("user", "userlinkedrole"), default="user")
    parser.add_argument("--status")
    parser.add_argument("--action", choices=("push", "recycle"), default="push")
    parser.add_argument("--sync-time", type=int, help="明确指定源版本，便于验证乱序和相同版本冲突")
    parser.add_argument("--retry", type=Path, help="重发已经留档的事件，不生成新的编号或版本")
    args = parser.parse_args()
    settings = Settings()
    if settings.auth_mode != "mock" or settings.app_env == "production":
        parser.error("requires AUTH_MODE=mock in development/test")
    try:
        if args.retry:
            if args.subject or args.status or args.sync_time is not None:
                parser.error("--retry cannot be combined with changes to the saved event")
            batch = SyncBatch.model_validate_json(args.retry.read_text(encoding="utf-8"))
            request_file = args.retry.resolve()
        else:
            if not args.subject or not args.status:
                parser.error("--subject and --status are required unless --retry is used")
            item = {"subject": args.subject, "status": args.status}
            if args.resource == "userlinkedrole":
                item["roleCode"] = "platform_admin"
            batch = SyncBatch.model_validate(
                {
                    "eventId": str(uuid4()),
                    "appId": "platform",
                    "resType": args.resource,
                    "action": args.action,
                    "syncTime": args.sync_time
                    if args.sync_time is not None
                    else int(time.time() * 1000),
                    "data": [item],
                }
            )
            request_file = (
                Path(__file__).resolve().parents[1]
                / ".runtime"
                / "identity-sync"
                / f"{batch.event_id}.json"
            )
            request_file.parent.mkdir(parents=True, exist_ok=True)
            # 留档只包含必要的模拟身份字段，不保存 IAM 密钥。
            with request_file.open("x", encoding="utf-8") as stream:
                stream.write(batch.model_dump_json(by_alias=True, indent=2))
    except (OSError, ValidationError) as error:
        parser.error(f"Invalid event file or fields ({type(error).__name__})")
    print(f"Event file: {request_file}")
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
            response = client.post(
                str(settings.auth_public_origin).rstrip("/") + "/api/v1/auth/mock/identity-sync",
                json=batch.model_dump(mode="json", by_alias=True),
                headers={"X-Mock-IAM-Key": settings.auth_mock_secret.get_secret_value()},
            )
        print(f"HTTP {response.status_code}")
        try:
            print(json.dumps(response.json(), ensure_ascii=False))
        except ValueError:
            print("The gateway did not return JSON.")
        if response.status_code != 200:
            raise SystemExit(1)
    except httpx.HTTPError:
        print("Delivery unconfirmed. Retry the same event file with --retry.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
