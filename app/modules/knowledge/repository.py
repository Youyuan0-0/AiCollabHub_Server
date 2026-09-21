"""知识库独立仓储；整库快照原子提交，避免文档、分段和统计分离更新。"""

from copy import deepcopy
from threading import RLock

from fastapi import HTTPException
from sqlalchemy import JSON, Engine, Integer, String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class KnowledgeRecord(Base):
    """保存知识库、文档元数据、分段和索引；原文文件存放在受控目录。"""

    __tablename__ = "knowledge_records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class KnowledgeRepository:
    """内存实例与共享数据库实现遵循相同的修订检查。"""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine
        self._records: dict[str, dict] = {}
        self._lock = RLock()

    def list(self) -> list[dict]:
        """读取独立快照，调用方无法修改仓储中的原始记录。"""
        if self.engine is not None:
            with Session(self.engine) as session:
                return deepcopy(list(session.scalars(select(KnowledgeRecord.data))))
        with self._lock:
            return deepcopy(list(self._records.values()))

    def get(self, identifier: str) -> dict | None:
        """按编号读取记录，未知编号返回 None。"""
        if self.engine is not None:
            with Session(self.engine) as session:
                row = session.get(KnowledgeRecord, identifier)
                return deepcopy(row.data) if row else None
        with self._lock:
            return deepcopy(self._records.get(identifier))

    def save(self, record: dict, expected: int | None) -> dict:
        """仅在修订匹配时提交；expected=None 表示新建，竞争失败返回 409。"""
        result = {**deepcopy(record), "revision": (expected or 0) + 1}
        conflict = HTTPException(409, "知识库已更新，请刷新后重试，当前输入未覆盖已保存内容")
        if self.engine is not None:
            try:
                with Session(self.engine) as session, session.begin():
                    if expected is None:
                        session.add(KnowledgeRecord(id=result["id"], revision=1, data=result))
                    else:
                        changed = session.execute(
                            update(KnowledgeRecord)
                            .where(
                                KnowledgeRecord.id == result["id"],
                                KnowledgeRecord.revision == expected,
                            )
                            .values(revision=result["revision"], data=result)
                        )
                        if changed.rowcount != 1:
                            raise conflict
            except IntegrityError as error:
                raise conflict from error
        else:
            with self._lock:
                current = self._records.get(result["id"])
                if (current["revision"] if current else None) != expected:
                    raise conflict
                self._records[result["id"]] = deepcopy(result)
        return result
