"""场景数据库与现有接口之间的基础值转换。"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5


def current_time() -> datetime:
    """返回带 UTC 时区的当前数据库业务时间。"""
    return datetime.now(UTC)


def date_value(value: str | datetime | None) -> datetime | None:
    """将 ISO 时间文本解析为日期对象，保留现有日期对象和空值。"""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def iso(value: datetime | None) -> str | None:
    """将日期转换为带微秒的 UTC 文本，空值保持为空。"""
    return value.astimezone(UTC).isoformat(timespec="microseconds") if value else None


def stable_id(kind: str, key: str) -> str:
    """根据资源种类与业务键生成稳定 UUID，保证重复导入时标识一致。"""
    return str(uuid5(NAMESPACE_URL, f"aicollabhub:scene:{kind}:{key}"))
