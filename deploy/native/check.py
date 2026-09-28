"""验证原生服务、业务表及应用账号读写权限，不输出连接凭据。"""

import json
import os
import re
import ssl
import subprocess
import sys
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from uuid import uuid4

import psycopg
import redis
from minio import Minio

ROOT = Path("/opt/aicollabhub")
CONFIG = Path("/etc/aicollabhub")


def main() -> None:
    """逐项输出验收结果；任一项失败即返回非零退出码。"""
    config = json.loads((CONFIG / "site.json").read_text())
    app_env = json.loads((CONFIG / "app-env.json").read_text())
    manifest = json.loads((ROOT / "current/manifest.json").read_text())
    os.environ.update(app_env)
    checks: dict[str, dict] = {}

    def check(name: str, action: Callable[[], object]) -> None:
        """隔离各项检查，失败只报告异常类型，防止泄露密码。"""
        try:
            value = action()
            checks[name] = {"ok": value is not False, "result": value}
        except Exception as error:
            checks[name] = {"ok": False, "errorType": type(error).__name__}

    def services() -> bool:
        """所有服务必须正在运行且设置开机启动。"""
        for name in ("postgres", "redis", "minio", "iam", "api", "web"):
            for mode in ("is-active", "is-enabled"):
                subprocess.run(["systemctl", mode, "--quiet", "aih-" + name], check=True)
        return True

    def database() -> dict:
        """应用账号检查 43 张业务表，并在临时表验证事务读写及权限限制。"""
        from app.db.foundation import COMMON_TABLES, RESOURCE_TABLES

        with psycopg.connect(
            host="127.0.0.1",
            port=config["postgres_port"],
            dbname=config["postgres_db"],
            user="aih_app",
            password=app_env["POSTGRES_PASSWORD"],
            connect_timeout=5,
        ) as connection:
            rows = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
            actual = {row[0] for row in rows}
            required = set(COMMON_TABLES)
            for main_table, prefix, _ in RESOURCE_TABLES.values():
                required.update((main_table, prefix + "_publications", prefix + "_versions"))
            if required - actual:
                raise RuntimeError("业务表不完整")
            if connection.execute(
                "SELECT has_schema_privilege(current_user,'public','CREATE')"
            ).fetchone()[0]:
                raise RuntimeError("应用账号不应拥有建表权限")
            connection.execute("CREATE TEMP TABLE aih_install_probe (id integer PRIMARY KEY)")
            connection.execute("INSERT INTO aih_install_probe VALUES (1)")
            connection.commit()
            if connection.execute("SELECT id FROM aih_install_probe").fetchone() != (1,):
                raise RuntimeError("数据库读写异常")
            settings = connection.execute(
                "SELECT current_setting('max_connections'), "
                "current_setting('superuser_reserved_connections'), "
                "current_setting('synchronous_commit')"
            ).fetchone()
            if settings != ("60", "3", "local"):
                raise RuntimeError("数据库连接配置不符合要求")
            version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if version != manifest["schema_head"]:
                raise RuntimeError("数据库未迁移到本安装包要求的版本")
            if app_env["APP_ENV"] != "test" or app_env["POSTGRES_DB"] != config["postgres_db"]:
                raise RuntimeError("当前应用未连接到配置指定的测试数据库")
            return {
                "databaseEnvironment": config["database_environment"],
                "databaseName": config["postgres_db"],
                "businessTables": len(required),
                "schemaVersion": version,
                "maxConnections": 60,
                "reservedConnections": 3,
                "appCanCreateTables": False,
            }

    def cache() -> bool:
        """使用独立随机缓存键验证 Redis，不触碰业务键。"""
        client = redis.Redis(
            host="127.0.0.1",
            port=config["redis_port"],
            password=app_env["REDIS_PASSWORD"],
            socket_timeout=5,
        )
        key = "aih-install-probe:" + uuid4().hex
        try:
            client.set(key, "offline-ok", ex=30)
            return client.get(key) == b"offline-ok"
        finally:
            client.delete(key)
            client.close()

    def storage() -> bool:
        """应用账号完成随机对象上传、读取、删除，检验专属桶权限。"""
        client = Minio(
            f"127.0.0.1:{config['minio_port']}",
            secure=False,
            access_key=app_env["MINIO_ACCESS_KEY"],
            secret_key=app_env["MINIO_SECRET_KEY"],
        )
        key = "install-probe/" + uuid4().hex
        bucket = app_env["MINIO_BUCKET"]
        try:
            client.put_object(bucket, key, BytesIO(b"offline-ok"), 10)
            response = client.get_object(bucket, key)
            try:
                return response.read() == b"offline-ok"
            finally:
                response.close()
                response.release_conn()
        finally:
            client.remove_object(bucket, key)

    def websites() -> dict:
        """通过真实网站入口检查两个 SPA、静态资源及 API 依赖健康。"""
        origin = config["public_origin"]
        context = ssl.create_default_context()
        if (CONFIG / "web.crt").exists():
            context.load_verify_locations(str(CONFIG / "web.crt"))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
        report = {}
        for route in ("/", "/admin/", "/admin/login", "/login", "/api/v1/health/dependencies"):
            with opener.open(origin + route, timeout=10) as response:
                body = response.read().decode()
                if response.status != 200:
                    raise RuntimeError("网站请求失败")
                if route.startswith("/api/"):
                    if json.loads(body)["status"] != "ready":
                        raise RuntimeError("API 依赖未就绪")
                else:
                    assets = re.findall(r'(?:src|href)="([^\"]+\.(?:js|css))"', body)
                    if not assets:
                        raise RuntimeError("前端构建资源缺失")
                    for asset in assets:
                        with opener.open(urljoin(origin + route, asset), timeout=10) as resource:
                            if (
                                resource.status != 200
                                or resource.headers.get_content_type() == "text/html"
                            ):
                                raise RuntimeError("静态文件被错误回退为 HTML")
                report[route] = 200
        return report

    check("systemd_services", services)
    check("postgresql_schema_and_permissions", database)
    check("redis_write_read_delete", cache)
    check("minio_write_read_delete", storage)
    check("websites_and_api", websites)
    passed = all(item["ok"] for item in checks.values())
    report = {
        "passed": passed,
        "profile": "rehearsal",
        "databaseEnvironment": "test",
        "releaseId": manifest["release_id"],
        "checks": checks,
    }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    (CONFIG / "last-check.json").write_text(output + "\n")
    (CONFIG / "last-check.json").chmod(0o600)
    print(output)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
