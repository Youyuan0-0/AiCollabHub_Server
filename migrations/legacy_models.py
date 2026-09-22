"""仅供历史迁移回归使用的旧表定义，业务运行不导入这些模型。"""

from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DatasetRecord(Base):
    """旧数据集快照表；新基线已拆分，不再用于平台读写。"""

    __tablename__ = "dataset_records"
    key: Mapped[str] = mapped_column(String(240), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
