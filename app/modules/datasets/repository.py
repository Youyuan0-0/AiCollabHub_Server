"""数据集申请与个人活动仓储；内存演示和数据库使用相同的修订号约束。"""

from copy import deepcopy
from threading import RLock

from fastapi import HTTPException
from sqlalchemy import JSON, Engine, Integer, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class DatasetRecord(Base):
    """保存用户端阶段的独立业务记录，key 前缀区分申请与单用户单资源活动。"""

    __tablename__ = "dataset_records"
    key: Mapped[str] = mapped_column(String(240), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class DatasetRepository:
    """有 engine 时使用事务和条件更新；否则提供线程安全、实例隔离的演示存储。"""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine
        self._records: dict[str, dict] = {}
        self._lock = RLock()

    def list(self, prefix: str) -> list[dict]:
        """返回指定业务前缀下的独立副本，调用方不能通过修改结果绕过版本校验。"""
        if self.engine is not None:
            with Session(self.engine) as session:
                return deepcopy(
                    list(
                        session.scalars(
                            select(DatasetRecord.data).where(DatasetRecord.key.startswith(prefix))
                        )
                    )
                )
        with self._lock:
            return deepcopy([data for key, data in self._records.items() if key.startswith(prefix)])

    def get(self, key: str) -> dict | None:
        """按内部业务键读取记录，不存在时返回 None。"""
        if self.engine is not None:
            with Session(self.engine) as session:
                row = session.get(DatasetRecord, key)
                return deepcopy(row.data) if row else None
        with self._lock:
            return deepcopy(self._records.get(key))

    def save(self, key: str, data: dict, expected: int | None) -> dict:
        """原子比较修订号并保存；expected=None 只允许新建，竞争失败返回 409。"""
        result = {**deepcopy(data), "revision": (expected or 0) + 1}
        conflict = HTTPException(409, "记录已更新，请刷新后重试；当前输入尚未覆盖服务端内容")
        if self.engine is not None:
            try:
                with Session(self.engine) as session, session.begin():
                    if expected is None:
                        session.add(DatasetRecord(key=key, revision=1, data=result))
                    else:
                        changed = session.execute(
                            update(DatasetRecord)
                            .where(
                                DatasetRecord.key == key,
                                DatasetRecord.revision == expected,
                            )
                            .values(revision=result["revision"], data=result)
                        )
                        if changed.rowcount != 1:
                            raise conflict
            except IntegrityError as error:
                raise conflict from error
        else:
            with self._lock:
                current = self._records.get(key)
                if (current["revision"] if current else None) != expected:
                    raise conflict
                self._records[key] = deepcopy(result)
        return result
