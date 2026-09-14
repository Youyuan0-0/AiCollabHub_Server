"""Read-only diagnostics by default; --write-probes uses uniquely named disposable test data."""

import argparse
import json
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from app.core.config import Settings
from app.core.resources import create_resources
from app.db.session import create_db_engine


def verify(settings, *, write_probes=False, create_bucket=False):
    report = {"checks": {}, "scope": "service integration; not capacity or failover acceptance"}
    checks = report["checks"]

    def check(name, action):
        try:
            result = action()
            checks[name] = {"ok": bool(result), "result": result}
            return result
        except Exception as error:
            checks[name] = {"ok": False, "errorType": type(error).__name__}
            return None

    resources = create_resources(settings)
    replica = None
    try:
        if resources.database:
            primary = resources.database.primary

            def primary_status():
                with primary.connect() as connection:
                    values = connection.execute(
                        text(
                            "SELECT pg_is_in_recovery(), "
                            "current_setting('synchronous_commit'), "
                            "current_setting('synchronous_standby_names')"
                        )
                    ).one()
                    if values[0]:
                        raise RuntimeError("Configured primary is a standby")
                    return {
                        "isPrimary": not values[0],
                        "synchronousCommit": values[1],
                        "synchronousStandbys": values[2],
                    }

            state = check("postgresql_primary", primary_status)
            if settings.database_read_url:
                replica = create_db_engine(
                    settings.database_read_url.get_secret_value(),
                    target_role="standby",
                    readonly=True,
                )
                check("postgresql_standby", lambda: _standby_status(replica))
            check("replication", lambda: _replication(primary))
            if write_probes:
                check(
                    "write_and_standby_visibility",
                    lambda: _database_probe(
                        primary,
                        replica,
                        bool(
                            state
                            and state["synchronousCommit"] == "remote_apply"
                            and state["synchronousStandbys"]
                        ),
                    ),
                )
        else:
            checks["postgresql_primary"] = {"ok": False, "errorType": "NotConfigured"}
        if resources.cache:
            check("redis_ping", resources.cache.probe)
            if write_probes:

                def cache_probe():
                    key = f"integration:{uuid4()}"
                    try:
                        return resources.cache.set_json(key, {"probe": 1}, ttl=30) and (
                            resources.cache.get_json(key) == {"probe": 1}
                        )
                    finally:
                        resources.cache.delete(key)

                check("redis_write_read_delete", cache_probe)
        else:
            checks["redis_ping"] = {"ok": False, "errorType": "NotConfigured"}
        if resources.storage:
            storage = resources.storage
            if create_bucket:

                def ensure_bucket():
                    if not storage.client.bucket_exists(storage.bucket):
                        storage.client.make_bucket(storage.bucket)
                    return True

                check("minio_bucket_create", ensure_bucket)
            check("minio_bucket", storage.probe)
            if write_probes:

                def storage_probe():
                    key = f"integration/{uuid4()}"
                    try:
                        storage.put_object(key, BytesIO(b"integration-probe"), length=17)
                        with storage.open_object(key) as response:
                            return response.read() == b"integration-probe"
                    finally:
                        storage.remove_object(key)

                check("minio_write_read_delete", storage_probe)
        else:
            checks["minio_bucket"] = {"ok": False, "errorType": "NotConfigured"}
    finally:
        if replica is not None:
            replica.dispose()
        resources.close()
    return report


def _standby_status(engine):
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT pg_is_in_recovery(), "
                "current_setting('transaction_read_only'), pg_last_wal_replay_lsn()::text"
            )
        ).one()
        if not row[0] or row[1] != "on":
            raise RuntimeError("Configured standby is not a read-only recovery server")
        return {"isStandby": row[0], "readOnly": row[1], "replayLsn": row[2]}


def _replication(engine):
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT application_name, state, sync_state, "
                    "sent_lsn::text, replay_lsn::text FROM pg_stat_replication"
                )
            )
            .mappings()
            .all()
        )
        if not any(row["state"] == "streaming" and row["sync_state"] == "sync" for row in rows):
            raise RuntimeError("A streaming synchronous standby has not been confirmed")
        return [dict(row) for row in rows]


def _database_probe(primary, replica, synchronous):
    # 创建独立随机名测试表，提交后从备库读取，最后在新的事务中清理。
    if replica is None or not synchronous:
        raise RuntimeError("Synchronous standby must be configured before write probe")
    name = "aicollab_probe_" + uuid4().hex
    try:
        with primary.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            connection.execute(text(f'CREATE TABLE "{name}" (id integer PRIMARY KEY)'))
            connection.execute(text(f'INSERT INTO "{name}" VALUES (1)'))
        with replica.connect() as connection:
            return connection.execute(text(f'SELECT id FROM "{name}"')).scalar_one() == 1
    finally:
        with primary.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--write-probes", action="store_true")
    parser.add_argument("--create-bucket", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(
        Settings(_env_file=args.env),
        write_probes=args.write_probes,
        create_bucket=args.create_bucket,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized)
    raise SystemExit(0 if all(item["ok"] for item in report["checks"].values()) else 1)


if __name__ == "__main__":
    main()
