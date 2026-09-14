"""首页运营快照模型，正式业务统计可通过服务层逐步替换快照来源。"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OverviewSnapshot(Base):
    """按板块与筛选键唯一保存快照；JSON 使用文本存储以减少数据库方言依赖。"""

    __tablename__ = "overview_snapshots"

    section: Mapped[str] = mapped_column(String(32), primary_key=True)
    variant: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # 数据库统一存无时区 UTC，读取时恢复 UTC 标记，避免不同数据库的时区差异。
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
