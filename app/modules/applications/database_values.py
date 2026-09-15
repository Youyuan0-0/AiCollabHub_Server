"""场景数据库与现有接口之间的基础值转换。"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5


def current_time():
    return datetime.now(UTC)


def date_value(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def iso(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds") if value else None


def stable_id(kind, key):
    return str(uuid5(NAMESPACE_URL, f"aicollabhub:scene:{kind}:{key}"))
