"""加载原前端首页样例，供非生产环境缺失快照时返回固定内容。"""

import json
import logging
from functools import cache
from importlib.resources import files

from fastapi import HTTPException, status

from app.schemas.overview import OverviewModel

logger = logging.getLogger(__name__)


@cache
def load_demo_data() -> dict:
    """缓存只读样例文件，避免每次请求读取磁盘；不生成时间戳或写入数据库。"""
    return json.loads(files("app.data").joinpath("overview_demo.json").read_text(encoding="utf-8"))


def demo_snapshot[T: OverviewModel](section: str, model: type[T], variant: str) -> T:
    """按板块和样例分类返回独立模型；文件损坏或结构错误仍返回 503。"""
    try:
        data = load_demo_data()[section]
        if variant != "default":
            data = data[variant]
        # 每次校验产生新的模型与容器，请求不能修改缓存中的固定内容。
        return model.model_validate(data)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Invalid overview demo data (%s)", section)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "overview_demo_invalid") from None
