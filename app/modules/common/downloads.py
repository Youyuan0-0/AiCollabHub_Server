"""资料传输记录；完成表示服务端已发送响应，不代表浏览器已写入本地磁盘。"""

import logging

from sqlalchemy import update

from app.db.foundation import utc_now
from app.modules.common.resource_database import ResourceDatabase

logger = logging.getLogger(__name__)


def complete_download(repository: ResourceDatabase, key: str) -> None:
    """响应发送成功后幂等确认下载；失败留存未完成记录，不伪造下载统计。"""
    try:
        with repository.transaction() as unit:
            table = unit.table("resource_downloads")
            unit.db.execute(
                update(table)
                .where(
                    table.c.resource_type == repository.kind,
                    table.c.idempotency_key == unit.download_key(key),
                    table.c.status == "link_issued",
                )
                .values(status="completed", completed_at=utc_now())
            )
    except Exception:
        logger.warning(
            "Download completion could not be recorded",
            extra={"event": "download_completion_failed"},
        )
