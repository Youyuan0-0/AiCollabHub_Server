"""华为目录查询编排；后台定期更新外部快照，平台审核资料保持独立。"""

import asyncio
import logging
from datetime import UTC, datetime
from threading import Event

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from app.modules.common.toolchain.catalog_store import CatalogStore
from app.modules.common.toolchain.contracts import fingerprint
from app.modules.common.toolchain.huawei import HuaweiCatalog, HuaweiClient

logger = logging.getLogger(__name__)


class ToolchainCatalogs:
    """按模块、服务环境和 API Key 范围隔离目录，失败时保留成功快照。"""

    def __init__(self, clients: dict[str, HuaweiClient], store: CatalogStore) -> None:
        self.clients = clients
        self.store = store
        self.stopping = Event()
        self.closed = asyncio.Event()

    def stop(self) -> None:
        """先通知分页查询停止，再唤醒轮询等待。"""
        self.stopping.set()
        self.closed.set()

    def connection_key(self, module: str) -> str:
        """凭据摘要参与隔离，API Key 原值不进入数据库或响应。"""
        settings = self.clients[module].settings
        key = settings.toolchain_api_key
        return fingerprint(
            [
                module,
                settings.toolchain_provider,
                settings.toolchain_mode,
                str(settings.toolchain_pop_url or "https://huawei.invalid"),
                fingerprint(key.get_secret_value() if key else "mock-api-key"),
            ]
        )

    def reference(self, module: str, identifier: str) -> dict:
        """外部编号独立于平台 UUID；华为来源不伪造阿里工作空间。"""
        settings = self.clients[module].settings
        return {
            "provider": "huawei-platform",
            "mode": settings.toolchain_mode,
            "environment": str(settings.toolchain_pop_url or "https://huawei.invalid"),
            "connectionId": self.connection_key(module),
            "workspace": "",
            "externalId": identifier,
        }

    def state(self, module: str) -> dict:
        """呈现查询状态与完整目录；未完成首次查询不标记为空目录成功。"""
        row = self.store.load(self.connection_key(module))
        now = datetime.now(UTC)
        refreshing = bool(row.get("lease_until") and row["lease_until"] > now)
        error = row.get("last_error", "")
        status = "pending"
        if refreshing:
            status = "refreshing"
        elif error:
            status = "error"
        elif row.get("last_synced_at"):
            status = "ready"
        settings = self.clients[module].settings
        return {
            "provider": "huawei",
            "mode": settings.toolchain_mode,
            "status": status,
            "items": row.get("data", {}).get("items", []),
            "lastError": error,
            "lastAttemptAt": row.get("last_attempt_at"),
            "lastSyncedAt": row.get("last_synced_at"),
            "nextPollAt": row.get("next_poll_at") if settings.toolchain_polling_enabled else None,
            "pollingEnabled": settings.toolchain_polling_enabled,
            "pollInterval": settings.toolchain_poll_interval,
        }

    def page(self, module: str, page: int, size: int, keyword: str) -> dict:
        """对同一成功快照分页，筛选不另行触发供应方全量查询。"""
        result = self.state(module)
        term = keyword.strip().casefold()
        items = [
            item
            for item in result["items"]
            if not term
            or any(
                term in str(item.get(field, "")).casefold()
                for field in ("name", "summary", "externalId")
            )
        ]
        return {
            **result,
            "items": items[(page - 1) * size : page * size],
            "total": len(items),
            "page": page,
            "pageSize": size,
        }

    def refresh(self, module: str, *, force: bool = False) -> dict:
        """领取查询租约后逐页续期；仅完整成功结果替换上次目录。"""
        client = self.clients[module]
        key = self.connection_key(module)
        lease = max(120, int(client.settings.toolchain_timeout * 2 + 30))
        token = self.store.claim(key, module, lease, force=force)
        if token:
            try:

                def renew() -> None:
                    if self.stopping.is_set():
                        raise HTTPException(503, "目录查询正在停止")
                    self.store.renew(key, token, lease)

                items = HuaweiCatalog(client).fetch(module, renew)
                self.store.finish(key, token, client.settings.toolchain_poll_interval, items=items)
            except HTTPException as error:
                self.store.finish(
                    key, token, client.settings.toolchain_poll_interval, error=str(error.detail)
                )
            except Exception:
                self.store.finish(
                    key,
                    token,
                    client.settings.toolchain_poll_interval,
                    error="外部目录查询未完成，请稍后重试",
                )
                logger.warning("toolchain catalog refresh failed module=%s", module)
        return self.state(module)

    async def maintain(self) -> None:
        """随应用生命周期启动；未到期、禁用或租约已被领取的连接不重复查询。"""
        while not self.stopping.is_set():
            for module, client in self.clients.items():
                if self.stopping.is_set():
                    return
                if client.settings.toolchain_polling_enabled:
                    try:
                        await run_in_threadpool(self.refresh, module)
                    except Exception:
                        logger.warning("toolchain catalog storage unavailable module=%s", module)
            try:
                await asyncio.wait_for(self.closed.wait(), timeout=5)
            except TimeoutError:
                continue
