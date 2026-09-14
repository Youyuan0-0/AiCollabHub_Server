"""Inspect or explicitly configure a connected PostgreSQL synchronous standby."""

import argparse
import json
import time
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.core.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--standby-name", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    if settings.database_url is None:
        parser.error("DATABASE_URL is required")
    url = make_url(settings.database_url.get_secret_value())
    if url.get_backend_name() != "postgresql":
        parser.error("This operation requires PostgreSQL")
    with psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=15000 -c lock_timeout=3000",
    ) as conn:
        if conn.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
            parser.error("The configured writer is a standby")
        before = dict(
            conn.execute(
                "SELECT name, setting FROM pg_settings "
                "WHERE name IN ('synchronous_standby_names', 'synchronous_commit')"
            ).fetchall()
        )
        existing = conn.execute(
            "SELECT setting FROM pg_db_role_setting s, unnest(s.setconfig) setting "
            "WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname=current_database()) "
            "AND s.setrole=0 AND setting LIKE 'synchronous_commit=%'"
        ).fetchall()
        before["databaseOverride"] = [r[0] for r in existing]
        standby = conn.execute(
            "SELECT state, sync_state FROM pg_stat_replication WHERE application_name=%s",
            (args.standby_name,),
        ).fetchall()
        if len(standby) != 1 or standby[0][0] != "streaming":
            parser.error("Exactly one connected streaming standby must match the supplied name")
        policy = 'FIRST 1 ("' + args.standby_name.replace('"', '""') + '")'
        if before["synchronous_standby_names"] not in ("", policy):
            parser.error("Existing synchronous standby policy differs; review it before changing")
        report = {"before": before, "standby": args.standby_name, "applied": False}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            parser.error("Use a new output path so the original settings remain recoverable")
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if args.apply:
            # 先确认备库正常流复制，再指定同步备库；保留原配置以便人工回退。
            conn.execute(
                sql.SQL("ALTER SYSTEM SET synchronous_standby_names = {}").format(
                    sql.Literal(policy)
                )
            )
            conn.execute("SELECT pg_reload_conf()")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row = conn.execute(
                    "SELECT sync_state FROM pg_stat_replication WHERE application_name=%s",
                    (args.standby_name,),
                ).fetchone()
                if row and row[0] == "sync":
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError(
                    "Standby did not enter synchronous state; inspect saved settings"
                )
            # remote_apply 仅设置到平台数据库，新连接在对应事务重放后才能确认提交。
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET synchronous_commit = 'remote_apply'").format(
                    sql.Identifier(url.database)
                )
            )
            report["applied"] = True
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
