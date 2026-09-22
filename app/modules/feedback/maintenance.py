"""按数据库领取记录的反馈暂存清理循环。"""

import asyncio
import logging

from fastapi.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.db.foundation import FoundationDatabase
from app.storage.maintenance import cleanup_files

logger = logging.getLogger(__name__)


async def maintain_feedback(
    resources: ApplicationResources, settings: Settings, stopping: asyncio.Event
) -> None:
    """每五分钟清理一批文件，停机时等待当前批次释放数据库与文件连接。"""

    def run_batch() -> None:
        if resources.database is not None and resources.storage is not None:
            if resources.database.dialect.name == "postgresql":
                cleanup_files(
                    FoundationDatabase(resources.database, settings), resources.storage, limit=10
                )

    while not stopping.is_set():
        try:
            await asyncio.wait_for(stopping.wait(), timeout=300)
            return
        except TimeoutError:
            pass
        try:
            await run_in_threadpool(run_batch)
        except Exception as error:
            logger.warning("File cleanup deferred (%s)", type(error).__name__)
