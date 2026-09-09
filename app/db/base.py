from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


# 业务 ORM 模型统一继承此基类，Alembic 通过其 metadata 获取表结构。
class Base(DeclarativeBase):
    # 固定索引和约束名称，避免迁移与回退依赖各数据库自动生成的名称。
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
