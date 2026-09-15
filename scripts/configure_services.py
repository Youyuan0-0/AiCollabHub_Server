"""从服务配置导入单库、Redis 和 MinIO 凭据，不输出密钥或修改来源文件。"""

import argparse
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", required=True, type=Path)
    parser.add_argument("--output-env", type=Path, default=Path(".env"))
    parser.add_argument("--host", default="127.0.0.1")
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
        "POSTGRES_PORT": values.get("POSTGRES_PORT") or "5432",
        "POSTGRES_DB": values.get("POSTGRES_DB") or "aicollabhub",
        "POSTGRES_USER": values.get("POSTGRES_USER") or "aicollabhub",
        "POSTGRES_PASSWORD": values["POSTGRES_PASSWORD"],
        "MINIO_ENDPOINT": f"http://{args.host}:{values.get('MINIO_API_PORT') or '9000'}",
        "MINIO_ACCESS_KEY": values.get("MINIO_ROOT_USER") or "aicollabadmin",
        "MINIO_SECRET_KEY": values["MINIO_ROOT_PASSWORD"],
        "MINIO_REGION": "us-east-1",
        "MINIO_BUCKET": values.get("MINIO_BUCKET") or "aicollabhub-files",
    }
    replaced_urls = ["DATABASE_URL"]
    if values.get("REDIS_PASSWORD"):
        settings.update(
            REDIS_HOST=values.get("REDIS_HOST") or args.host,
            REDIS_PORT=values.get("REDIS_PORT") or "6379",
            REDIS_USER=values.get("REDIS_USER") or "",
            REDIS_PASSWORD=values["REDIS_PASSWORD"],
            REDIS_DB=values.get("REDIS_DB") or "0",
            REDIS_SSL=values.get("REDIS_SSL") or "false",
        )
        replaced_urls.append("REDIS_URL")
    args.output_env.parent.mkdir(parents=True, exist_ok=True)
    for key, value in settings.items():
        set_key(args.output_env, key, value)
    # URL 优先级更高，写入分字段配置后清除对应旧值，避免覆盖新地址。
    for key in replaced_urls:
        if key in dotenv_values(args.output_env):
            unset_key(args.output_env, key)
    print(f"Updated {len(settings)} application settings. Credentials were not printed.")


if __name__ == "__main__":
    main()
