"""个人中心列表的查询契约。"""

from typing import Annotated, Literal

from pydantic import Field

from app.modules.common.schemas import PageQuery, Payload


class ProfileQuery(PageQuery):
    """个人列表按资源类型、业务状态和关键词过滤。"""

    kind: Literal["", "dataset", "model", "application", "knowledge", "tool"] = ""
    status: str = ""


class ReadMessages(Payload):
    """明确指定待标记的消息，每批最多 100 条以限制原子操作时长。"""

    ids: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1,
        max_length=100,
    )
