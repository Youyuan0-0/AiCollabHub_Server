"""外部目录快照和轮询租约；与平台正式资源、文件及审核资料分别保存。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine


def catalog_table(schema: str | None = None) -> Table:
    """声明目录表结构；业务启动仅使用已有表，不执行建表。"""
    return Table(
        "toolchain_catalog_snapshots",
        MetaData(schema=schema),
        Column("connection_key", String(64), primary_key=True),
        Column("module_code", String(32), nullable=False),
        Column("provider", String(32), nullable=False),
        Column("data", JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        Column("last_attempt_at", DateTime(timezone=True)),
        Column("last_synced_at", DateTime(timezone=True)),
        Column("next_poll_at", DateTime(timezone=True)),
        Column("last_error", String(500), nullable=False),
        Column("failure_count", Integer, nullable=False),
        Column("lease_token", String(32)),
        Column("lease_until", DateTime(timezone=True)),
    )


def aware(value: datetime | None) -> datetime | None:
    """兼容测试数据库返回的无时区值，正式存储统一使用 UTC。"""
    return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class CatalogStore:
    """数据库保存成功快照；租约避免多个 worker 重复轮询同一连接。

    未配置数据库的开发实例使用进程内目录。配置数据库后缺表会明确失败，
    不通过内存回退掩盖未执行迁移的问题。租约每页续期，过期工作者不能提交结果。
    """

    def __init__(self, engine: Engine | None = None, *, schema: str | None = None) -> None:
        self.engine = engine
        self.table = catalog_table(schema)
        self.memory: dict[str, dict] = {}
        self.lock = RLock()
        if engine and engine.dialect.name not in ("postgresql", "sqlite"):
            raise RuntimeError("外部目录快照仅支持 PostgreSQL 数据库")
        if engine and not inspect(engine).has_table(self.table.name, schema=schema):
            raise RuntimeError(
                "华为目录查询需要先执行资源库迁移，创建 toolchain_catalog_snapshots 表"
            )

    def load(self, key: str) -> dict:
        """返回快照及查询时间；失败查询仍保留上次成功的数据。"""
        if self.engine:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(select(self.table).where(self.table.c.connection_key == key))
                    .mappings()
                    .first()
                )
                result = dict(row) if row else {}
        else:
            with self.lock:
                result = deepcopy(self.memory.get(key, {}))
        for field in ("last_attempt_at", "last_synced_at", "next_poll_at", "lease_until"):
            if field in result:
                result[field] = aware(result[field])
        return result

    def claim(
        self, key: str, module: str, lease_seconds: int, *, force: bool = False
    ) -> str | None:
        """原子领取到期查询；网络请求在数据库事务提交之后执行。"""
        now, token = datetime.now(UTC), uuid4().hex
        initial = {
            "connection_key": key,
            "module_code": module,
            "provider": "huawei",
            "data": {"items": []},
            "last_error": "",
            "failure_count": 0,
        }
        values = {
            "lease_token": token,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "last_attempt_at": now,
        }
        if self.engine:
            factory = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
            with self.engine.begin() as connection:
                connection.execute(factory(self.table).values(**initial).on_conflict_do_nothing())
                statement = update(self.table).where(
                    self.table.c.connection_key == key,
                    or_(self.table.c.lease_until.is_(None), self.table.c.lease_until <= now),
                )
                if not force:
                    statement = statement.where(
                        or_(self.table.c.next_poll_at.is_(None), self.table.c.next_poll_at <= now)
                    )
                return token if connection.execute(statement.values(**values)).rowcount else None
        with self.lock:
            row = self.memory.setdefault(key, initial)
            if row.get("lease_until") and row["lease_until"] > now:
                return None
            if not force and row.get("next_poll_at") and row["next_poll_at"] > now:
                return None
            row.update(values)
            return token

    def renew(self, key: str, token: str, lease_seconds: int) -> None:
        """分页前续租；租约已被新工作者接管时中止旧查询。"""
        now = datetime.now(UTC)
        values = {"lease_until": now + timedelta(seconds=lease_seconds)}
        if self.engine:
            with self.engine.begin() as connection:
                changed = connection.execute(
                    update(self.table)
                    .where(
                        self.table.c.connection_key == key,
                        self.table.c.lease_token == token,
                        self.table.c.lease_until > now,
                    )
                    .values(**values)
                ).rowcount
        else:
            with self.lock:
                row = self.memory[key]
                changed = row.get("lease_token") == token and row["lease_until"] > now
                if changed:
                    row.update(values)
        if not changed:
            raise HTTPException(409, "目录查询已由其他工作者接管")

    def finish(
        self,
        key: str,
        token: str,
        interval: int,
        *,
        items: list[dict] | None = None,
        error: str = "",
    ) -> None:
        """成功时替换完整目录；失败时只更新错误和退避时间，不清空原目录。"""
        now = datetime.now(UTC)
        row = self.load(key)
        failures = row.get("failure_count", 0) + 1 if error else 0
        delay = min(interval * 2 ** min(failures, 4), max(interval, 3600)) if error else interval
        values = {
            "last_error": error[:500],
            "failure_count": failures,
            "next_poll_at": now + timedelta(seconds=delay),
            "lease_token": None,
            "lease_until": None,
        }
        if items is not None and not error:
            values.update(data={"items": items}, last_synced_at=now)
        if self.engine:
            with self.engine.begin() as connection:
                connection.execute(
                    update(self.table)
                    .where(
                        self.table.c.connection_key == key,
                        self.table.c.lease_token == token,
                        self.table.c.lease_until > now,
                    )
                    .values(**values)
                )
        else:
            with self.lock:
                row = self.memory[key]
                if row.get("lease_token") == token and row["lease_until"] > now:
                    row.update(values)
