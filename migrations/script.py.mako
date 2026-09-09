"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    # 自动生成的迁移须人工核对，尤其是目标数据库的类型和约束差异。
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    # 回退可能删除字段或数据，执行前需确认备份和可逆性。
    ${downgrades if downgrades else "pass"}
