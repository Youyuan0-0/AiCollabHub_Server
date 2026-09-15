"""默认只读检查单库、Redis 和 MinIO；写探测使用随机测试数据并清理。"""

import argparse
import json
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from app.core.config import Settings
from app.core.resources import create_resources
from app.db.base import Base
from app.db.conversations import DemoConversation  # noqa: F401
from app.db.overview import OverviewSnapshot  # noqa: F401
from app.modules.common.auth import models  # noqa: F401
from app.modules.common.identity_sync import models as identity_models  # noqa: F401


def verify(settings, *, write_probes=False, create_bucket=False):
    report = {"checks": {}, "scope": "single-server service integration; not capacity acceptance"}
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
    try:
        if resources.database is not None:
            engine = resources.database

            def database_status():
                with engine.connect() as connection:
                    values = connection.execute(
                        text(
                            "SELECT current_setting('transaction_read_only'), "
                            "current_setting('synchronous_commit')"
                        )
                    ).one()
                    if values != ("off", "local"):
                        raise RuntimeError("Database must allow writes and local durable commits")
                    return {"readOnly": False, "synchronousCommit": values[1]}

            check("postgresql", database_status)

            def database_schema():
                actual = set(inspect(engine).get_table_names())
                return {"missingTables": sorted(set(Base.metadata.tables) - actual)}

            schema = check("database_schema", database_schema)
            if schema is not None:
                checks["database_schema"]["ok"] = not schema["missingTables"]
            if write_probes:
                check("database_write_read_delete", lambda: _database_probe(engine))
        else:
            checks["postgresql"] = {"ok": False, "errorType": "NotConfigured"}
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
        resources.close()
    return report


def _database_probe(engine):
    # 一个连接提交，另一个连接立即查询，验证持久化而非进程内状态。
    name = "aicollab_probe_" + uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            connection.execute(text(f'CREATE TABLE "{name}" (id integer PRIMARY KEY)'))
            connection.execute(text(f'INSERT INTO "{name}" VALUES (1)'))
        with engine.connect() as connection:
            return connection.execute(text(f'SELECT id FROM "{name}"')).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-probes", action="store_true")
    parser.add_argument("--create-bucket", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify(
        Settings(),
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
