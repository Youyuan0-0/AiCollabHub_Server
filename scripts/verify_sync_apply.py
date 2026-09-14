"""Bounded replay-pause acceptance for an explicitly configured test database."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import create_db_engine
from app.db.transactions import commit_write, prepare_write


def connect(url):
    value = make_url(url.get_secret_value())
    return psycopg.connect(
        host=value.host,
        port=value.port,
        dbname=value.database,
        user=value.username,
        password=value.password,
        connect_timeout=5,
        autocommit=True,
        options="-c statement_timeout=15000",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pause-standby-for-test", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    if settings.app_env != "test" or not settings.db_require_sync_apply:
        parser.error("APP_ENV=test and DB_REQUIRE_SYNC_APPLY=true are required")
    if not settings.database_url or not settings.database_read_url:
        parser.error("Primary and standby URLs are required")
    name = "aicollab_replay_probe_" + uuid4().hex
    table = sql.Identifier(name)
    engine = create_db_engine(
        settings.database_url.get_secret_value(), target_role="primary", pool_size=1, max_overflow=0
    )
    report = {"scope": "test-only replay pause; not capacity or physical-server failover"}
    try:
        with (
            connect(settings.database_url) as primary,
            connect(settings.database_read_url) as standby,
        ):
            policy = primary.execute(
                "SELECT pg_is_in_recovery(), current_setting('synchronous_commit'), "
                "current_setting('synchronous_standby_names')"
            ).fetchone()
            if policy[0] or policy[1] != "remote_apply" or not policy[2]:
                parser.error("The primary must already require synchronous remote_apply")
            if not standby.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                parser.error("The read endpoint must be a standby")
            if standby.execute("SELECT pg_is_wal_replay_paused()").fetchone()[0]:
                parser.error("Standby was already paused; it was left unchanged")
            primary.execute(sql.SQL("CREATE TABLE {} (id integer PRIMARY KEY)").format(table))

            def write(record_id, timeout_ms):
                with Session(engine) as session:
                    prepare_write(
                        session,
                        settings.model_copy(
                            update={
                                "db_statement_timeout_ms": timeout_ms,
                                "db_commit_timeout_ms": timeout_ms,
                            }
                        ),
                    )
                    session.execute(
                        text("SELECT set_config('application_name', :name, true)"), {"name": name}
                    )
                    session.execute(text(f'INSERT INTO "{name}" VALUES (:id)'), {"id": record_id})
                    started = time.monotonic()
                    try:
                        commit_write(session)
                        return {
                            "outcome": "confirmed",
                            "commitMs": round((time.monotonic() - started) * 1000),
                        }
                    except HTTPException as error:
                        return {"outcome": error.detail, "httpStatus": error.status_code}

            def pause():
                standby.execute("SELECT pg_wal_replay_pause()")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if (
                        standby.execute("SELECT pg_get_wal_replay_pause_state()").fetchone()[0]
                        == "paused"
                    ):
                        return
                    time.sleep(0.05)
                raise RuntimeError("Standby did not reach paused state")

            # 每次故障注入都用独立 finally 恢复，提交等待同时受独立提交超时限制。
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    try:
                        pause()
                        future = executor.submit(write, 1, 8000)
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            waiting = primary.execute(
                                "SELECT 1 FROM pg_stat_activity "
                                "WHERE application_name=%s AND wait_event='SyncRep'",
                                (name,),
                            ).fetchone()
                            if waiting or future.done():
                                break
                            time.sleep(0.05)
                        if not waiting or future.done():
                            raise RuntimeError(
                                "Write was not observed waiting for synchronous replay"
                            )
                        report["waitedWhileReplayPaused"] = True
                    finally:
                        standby.execute("SELECT pg_wal_replay_resume()")
                    report["resumedWrite"] = future.result(timeout=10)
                    if report["resumedWrite"]["outcome"] != "confirmed":
                        raise RuntimeError("Write did not confirm after replay resumed")
                    if standby.execute(
                        sql.SQL("SELECT id FROM {} WHERE id=1").format(table)
                    ).fetchone() != (1,):
                        raise RuntimeError("Confirmed write was not immediately visible on standby")
                    report["immediatelyVisibleOnStandby"] = True
                    try:
                        pause()
                        future = executor.submit(write, 2, 1000)
                        result = future.result(timeout=6)
                        if result.get("outcome") != "commit_outcome_unknown":
                            raise RuntimeError(
                                "Cancelled sync wait was incorrectly treated as confirmed"
                            )
                        report["cancelledWait"] = result
                        report["cancelledCommitExistsOnPrimary"] = bool(
                            primary.execute(
                                sql.SQL("SELECT 1 FROM {} WHERE id=2").format(table)
                            ).fetchone()
                        )
                        if not report["cancelledCommitExistsOnPrimary"]:
                            raise RuntimeError(
                                "Expected committed local row after sync-wait cancellation"
                            )
                    finally:
                        standby.execute("SELECT pg_wal_replay_resume()")
                    # 按同一业务身份核对并再次提交，证明不会把一次不确定结果直接当成同步成功。
                    with Session(engine) as session:
                        prepare_write(session, settings)
                        session.execute(text(f'UPDATE "{name}" SET id=id WHERE id=2'))
                        commit_write(session)
                    report["retryConfirmedWithoutDuplicate"] = (
                        standby.execute(
                            sql.SQL("SELECT count(*) FROM {} WHERE id=2").format(table)
                        ).fetchone()[0]
                        == 1
                    )
                    with engine.connect() as reused:
                        reused.execute(text("SELECT pg_sleep(0.2)"))
                        report["pooledConnectionReusableAfterCancellation"] = (
                            reused.scalar(text("SELECT 1")) == 1
                        )
            finally:
                standby.execute("SELECT pg_wal_replay_resume()")
                primary.execute(sql.SQL("DROP TABLE {} ").format(table))
            report["replayResumedAtEnd"] = not standby.execute(
                "SELECT pg_is_wal_replay_paused()"
            ).fetchone()[0]
    finally:
        engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
