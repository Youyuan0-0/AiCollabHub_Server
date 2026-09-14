"""Logical backup and isolated restore verification using PostgreSQL client tools."""

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, help="Directory containing pg_dump and pg_restore")
    parser.add_argument("--restore-check", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("DATABASE_URL is required")
    url = make_url(settings.database_url.get_secret_value())
    if url.get_backend_name() != "postgresql":
        parser.error("This backup workflow requires PostgreSQL")
    env = {
        **os.environ,
        "PGHOST": url.host or "127.0.0.1",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": "5",
    }
    args.directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    archive = args.directory.resolve() / f"aicollabhub_{timestamp}.dump"

    def run(tool, *arguments):
        executable = str(args.pg_bin / tool) if args.pg_bin else tool
        result = subprocess.run(
            [executable, *map(str, arguments)], env=env, capture_output=True, timeout=600
        )
        if result.returncode:
            raise RuntimeError(f"{tool} failed (exit {result.returncode}); no credentials printed")
        return result.stdout

    def inventory(conn):
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY schemaname, tablename"
        ).fetchall()
        return {
            f"{schema}.{table}": conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, table))
            ).fetchone()[0]
            for schema, table in tables
        }

    # 计数与 pg_dump 使用同一个只读快照，避免备份期间新增记录造成核对误报。
    with psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        connect_timeout=5,
    ) as source:
        source.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        source.read_only = True
        snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
        source_counts = inventory(source)
        run(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--snapshot",
            snapshot,
            "--file",
            archive,
        )
    run("pg_restore", "--list", archive)
    digest = hashlib.sha256()
    with archive.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    manifest = {
        "file": archive.name,
        "sha256": digest.hexdigest(),
        "createdAt": timestamp,
        "restoreVerified": False,
        "sourceCounts": source_counts,
        "includes": "schema and data; roles, WAL and MinIO require separate backup",
    }
    if args.restore_check:
        # 只创建全新的隔离恢复库，不清理或覆盖任何现有数据库；恢复后保留供人工核对。
        target = "aicollab_restore_" + timestamp.lower()
        with psycopg.connect(
            host=url.host,
            port=url.port,
            dbname=url.database,
            user=url.username,
            password=url.password,
            connect_timeout=5,
            autocommit=True,
        ) as conn:
            conn.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target))
            )
        run("pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", target, archive)
        with psycopg.connect(
            host=url.host,
            port=url.port,
            dbname=target,
            user=url.username,
            password=url.password,
            connect_timeout=5,
        ) as conn:
            manifest["restoredCounts"] = inventory(conn)
        if manifest["restoredCounts"] != source_counts:
            raise RuntimeError("Restored row counts differ from the backup snapshot")
        manifest.update(restoreVerified=True, restoreDatabase=target)
    archive.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
