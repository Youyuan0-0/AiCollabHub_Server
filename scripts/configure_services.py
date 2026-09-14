"""Import only required service credentials without printing them or modifying their source."""

import argparse
from pathlib import Path

from dotenv import dotenv_values, set_key
from sqlalchemy.engine import URL


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
        "DATABASE_URL": URL.create(
            "postgresql+psycopg",
            username="aicollab",
            password=values["POSTGRES_PASSWORD"],
            host=args.host,
            port=15433,
            database="AiCollabHubTest",
        ).render_as_string(hide_password=False),
        "DATABASE_READ_URL": URL.create(
            "postgresql+psycopg",
            username="aicollab",
            password=values["POSTGRES_PASSWORD"],
            host=args.host,
            port=15434,
            database="AiCollabHubTest",
        ).render_as_string(hide_password=False),
        "MINIO_ENDPOINT": f"http://{args.host}:19002",
        "MINIO_ACCESS_KEY": "aicollabadmin",
        "MINIO_SECRET_KEY": values["MINIO_ROOT_PASSWORD"],
        "MINIO_REGION": "us-east-1",
        "MINIO_BUCKET": "aicollabhub-files",
        "DB_READ_ENABLED": "false",
        "DB_REQUIRE_SYNC_APPLY": "false",
        "DB_READ_FALLBACK_ENABLED": "false",
    }
    if values.get("REDIS_PASSWORD"):
        settings["REDIS_URL"] = URL.create(
            "redis",
            password=values["REDIS_PASSWORD"],
            host=args.host,
            port=int(values.get("REDIS_PORT") or 16379),
            database="0",
        ).render_as_string(hide_password=False)
    args.output_env.parent.mkdir(parents=True, exist_ok=True)
    for key, value in settings.items():
        set_key(args.output_env, key, value)
    print(f"Updated {len(settings)} application settings. Credentials were not printed.")
    print("Read routing remains disabled until synchronous standby verification succeeds.")


if __name__ == "__main__":
    main()
