"""Minimal shared CAS session, login state and audit storage."""

import sqlalchemy as sa
from alembic import op

revision = "20260914_01"
down_revision = "20260913_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_session",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("ticket_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(200)),
        sa.Column("display_name", sa.String(100)),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger()),
    )
    op.create_index("ix_auth_session_expires_at", "auth_session", ["expires_at"])
    op.create_table(
        "auth_login_request",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("service_url", sa.Text(), nullable=False),
        sa.Column("return_path", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed_at", sa.BigInteger()),
    )
    op.create_index("ix_auth_login_request_expires_at", "auth_login_request", ["expires_at"])
    op.create_table(
        "auth_event_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(200)),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("result", sa.String(40), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_auth_event_log_created_at", "auth_event_log", ["created_at"])
    op.create_index(
        "ix_auth_event_log_subject_created", "auth_event_log", ["subject", "created_at"]
    )


def downgrade():
    op.drop_table("auth_event_log")
    op.drop_table("auth_login_request")
    op.drop_table("auth_session")
