"""Import only required service credentials without printing them or modifying their source."""

import argparse
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", required=True, type=Path)
    parser.add_argument("--output-env", type=Path, default=Path(".env"))
    parser.add_argument("--host", default="192.168.0.219")
    args = parser.parse_args()
    if not args.source_env.is_file():
        parser.error("Service credential file is not accessible on this computer")
    if args.source_env.resolve() == args.output_env.resolve():
        parser.error("Source and application environment files must differ")
    values = dotenv_values(args.source_env)
    if not values.get("POSTGRES_PASSWORD") or not values.get("MINIO_ROOT_PASSWORD"):
        parser.error("POSTGRES_PASSWORD and MINIO_ROOT_PASSWORD are required")
    settings = {
        "POSTGRES_HOST": args.host,
        "POSTGRES_PORT": values.get("POSTGRES_A_PORT") or values.get("POSTGRES_PORT") or "15433",
        "POSTGRES_DB": values.get("POSTGRES_DB") or "AiCollabHubTest",
        "POSTGRES_USER": values.get("POSTGRES_USER") or "aicollab",
        "POSTGRES_PASSWORD": values["POSTGRES_PASSWORD"],
        "POSTGRES_READ_HOST": values.get("POSTGRES_READ_HOST") or args.host,
        "POSTGRES_READ_PORT": values.get("POSTGRES_READ_PORT")
        or values.get("POSTGRES_B_PORT")
        or "15434",
        "POSTGRES_READ_DB": values.get("POSTGRES_READ_DB")
        or values.get("POSTGRES_DB")
        or "AiCollabHubTest",
        "POSTGRES_READ_USER": values.get("POSTGRES_READ_USER")
        or values.get("POSTGRES_USER")
        or "aicollab",
        "POSTGRES_READ_PASSWORD": values.get("POSTGRES_READ_PASSWORD")
        or values["POSTGRES_PASSWORD"],
        "MINIO_ENDPOINT": f"http://{args.host}:19002",
        "MINIO_ACCESS_KEY": "aicollabadmin",
        "MINIO_SECRET_KEY": values["MINIO_ROOT_PASSWORD"],
        "MINIO_REGION": "us-east-1",
        "MINIO_BUCKET": "aicollabhub-files",
        "DB_READ_ENABLED": "false",
        "DB_REQUIRE_SYNC_APPLY": "false",
        "DB_READ_FALLBACK_ENABLED": "false",
    }
    replaced_urls = ["DATABASE_URL", "DATABASE_READ_URL"]
    if values.get("REDIS_PASSWORD"):
        settings.update(
            REDIS_HOST=values.get("REDIS_HOST") or args.host,
            REDIS_PORT=values.get("REDIS_PORT") or "16379",
            REDIS_USER=values.get("REDIS_USER") or "",
            REDIS_PASSWORD=values["REDIS_PASSWORD"],
            REDIS_DB=values.get("REDIS_DB") or "0",
            REDIS_SSL=values.get("REDIS_SSL") or "false",
        )
        replaced_urls.append("REDIS_URL")
    args.output_env.parent.mkdir(parents=True, exist_ok=True)
    for key, value in settings.items():
        set_key(args.output_env, key, value)
    # 旧 URL 优先级更高，完成分字段写入后清除对应旧值，避免继续连接旧服务。
    for key in replaced_urls:
        if key in dotenv_values(args.output_env):
            unset_key(args.output_env, key)
    print(f"Updated {len(settings)} application settings. Credentials were not printed.")
    print("Read routing remains disabled until synchronous standby verification succeeds.")


if __name__ == "__main__":
    main()
