"""清理到期的反馈暂存附件；多实例使用数据库记录协调领取。"""

import json

from app.core.config import Settings
from app.core.resources import create_resources
from app.db.foundation import FoundationDatabase
from app.storage.maintenance import cleanup_files


def main() -> None:
    """供计划任务周期执行，一次最多处理 100 个暂存附件。"""
    settings = Settings()
    resources = create_resources(settings)
    try:
        if resources.database is None or resources.storage is None:
            raise SystemExit("请先配置数据库与 MinIO")
        print(
            json.dumps(
                cleanup_files(FoundationDatabase(resources.database, settings), resources.storage)
            )
        )
    finally:
        resources.close()


if __name__ == "__main__":
    main()
