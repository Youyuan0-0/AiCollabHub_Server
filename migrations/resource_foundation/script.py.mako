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
    """执行本次结构升级；生成后需核对已有数据与约束。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回退本次结构变更，执行前确认备份与数据保留要求。"""
    ${downgrades if downgrades else "pass"}
