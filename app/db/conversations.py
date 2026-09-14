"""开发问答历史持久化；与未来正式用户会话表分离。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DemoConversation(Base):
    """临时保存完整会话 JSON 与删除标记；兼容旧单轮记录，后续可拆分正式消息表。"""

    __tablename__ = "demo_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
