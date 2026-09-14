from typing import Literal

from pydantic import Field

from app.modules.common.schemas import PageQuery, Payload


class FeedbackQuery(PageQuery):
    status: Literal["all", "open", "resolved"] = "all"


class FeedbackUpdate(Payload):
    status: Literal["处理中", "已解决"]
    reply: str = Field(min_length=1, max_length=5000)
