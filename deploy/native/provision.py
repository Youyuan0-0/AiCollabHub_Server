"""使用仅 root 可读的安装凭据完成数据库迁移和存储账号初始化。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from minio import Minio
from minio.credentials import StaticProvider
from minio.minioadmin import MinioAdmin
from psycopg import sql


def main() -> None:
    """创建独立迁移账号和应用账号，并执行新资源基线迁移。"""
    config = json.loads(Path("/etc/aicollabhub/site.json").read_text())
    credentials = json.loads(Path("/etc/aicollabhub/credentials.json").read_text())
    database_name = config["postgres_db"]
    database_identifier = sql.Identifier(database_name)
    options = {
        "host": "127.0.0.1",
        "port": config["postgres_port"],
        "user": "postgres",
        "password": credentials["postgres_admin_password"],
        "connect_timeout": 10,
        "autocommit": True,
    }
    with psycopg.connect(dbname="postgres", **options) as connection:
        for name, password_key in (
            ("aih_owner", "postgres_owner_password"),
            ("aih_app", "postgres_app_password"),
        ):
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                    ).format(sql.Identifier(name), sql.Literal(credentials[password_key]))
                )
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database_name,)
        ).fetchone():
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER aih_owner ENCODING 'UTF8'").format(
                    database_identifier
                )
            )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database_identifier)
        )
        connection.execute(
            sql.SQL("GRANT CONNECT,TEMPORARY ON DATABASE {} TO aih_app").format(database_identifier)
        )
    with psycopg.connect(dbname=database_name, **options) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute("GRANT USAGE,CREATE ON SCHEMA public TO aih_owner")
        connection.execute("GRANT USAGE ON SCHEMA public TO aih_app")
    env = {
        **os.environ,
        "POSTGRES_MIGRATION_HOST": "127.0.0.1",
        "POSTGRES_MIGRATION_PORT": str(config["postgres_port"]),
        "POSTGRES_MIGRATION_DB": database_name,
        "POSTGRES_MIGRATION_USER": "aih_owner",
        "POSTGRES_MIGRATION_PASSWORD": credentials["postgres_owner_password"],
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "-n", "resource_foundation", "upgrade", "head"],
        env=env,
        check=True,
    )
    with psycopg.connect(dbname=database_name, **options) as connection:
        connection.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO aih_app"
        )
        connection.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO aih_app")
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE aih_owner IN SCHEMA public "
            "GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO aih_app"
        )
        connection.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE aih_owner IN SCHEMA public "
            "GRANT USAGE,SELECT ON SEQUENCES TO aih_app"
        )
        connection.execute("REVOKE INSERT,UPDATE,DELETE ON alembic_version FROM aih_app")
    endpoint = f"127.0.0.1:{config['minio_port']}"
    storage = Minio(
        endpoint,
        access_key=credentials["minio_root_user"],
        secret_key=credentials["minio_root_password"],
        secure=False,
    )
    bucket = "aicollabhub-files"
    if not storage.bucket_exists(bucket):
        storage.make_bucket(bucket)
    admin = MinioAdmin(
        endpoint=endpoint,
        secure=False,
        credentials=StaticProvider(
            credentials["minio_root_user"], credentials["minio_root_password"]
        ),
    )
    admin.user_add(credentials["minio_app_user"], credentials["minio_app_password"])
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:ListBucketMultipartUploads",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            },
        ],
    }
    admin.policy_add("aih-platform-files", policy=policy)
    admin.policy_set("aih-platform-files", user=credentials["minio_app_user"])
    print("资源基线迁移、应用 DML 权限及指定存储桶访问权限已就绪。")


if __name__ == "__main__":
    main()
