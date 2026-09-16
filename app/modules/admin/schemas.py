from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.modules.common.schemas import PageQuery, Payload


class FeedbackQuery(PageQuery):
    """管理端反馈列表的分页、关键词和状态筛选。"""

    status: Literal["all", "open", "resolved"] = "all"


class FeedbackUpdate(Payload):
    """管理端反馈处理结果与回复内容。"""

    status: Literal["处理中", "已解决"]
    reply: str = Field(min_length=1, max_length=5000)
