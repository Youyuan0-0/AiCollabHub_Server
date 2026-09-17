"""Add persistent feedback, attachments and processing history."""

import sqlalchemy as sa
from alembic import op

revision = "20260917_01"
down_revision = "20260916_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedback_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submitter_id", sa.String(length=200), nullable=False),
        sa.Column("submitter_name", sa.String(length=100), nullable=False),
        sa.Column("submitter_org_id", sa.String(length=200), nullable=True),
        sa.Column("feedback_type", sa.String(length=32), nullable=False),
        sa.Column("type_other", sa.String(length=200), nullable=True),
        sa.Column("module_code", sa.String(length=32), nullable=False),
        sa.Column("module_other", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=60), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contact", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'processing' AND resolved_at IS NULL) OR (status = "
            "'resolved' AND resolved_at IS NOT NULL)",
            name=op.f("ck_feedback_records_feedback_resolution"),
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'resolved')", name=op.f("ck_feedback_records_feedback_status")
        ),
        sa.CheckConstraint(
            "length(trim(contact)) BETWEEN 1 AND 200",
            name=op.f("ck_feedback_records_feedback_contact"),
        ),
        sa.CheckConstraint(
            "length(trim(description)) BETWEEN 1 AND 5000",
            name=op.f("ck_feedback_records_feedback_description"),
        ),
        sa.CheckConstraint(
            "length(trim(title)) BETWEEN 1 AND 60", name=op.f("ck_feedback_records_feedback_title")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_feedback_records_feedback_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback_records")),
        sa.UniqueConstraint("submitter_id", "client_request_id", name="uq_feedback_submission"),
    )
    op.create_index(
        "ix_feedback_owner_time",
        "feedback_records",
        ["submitter_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_feedback_status_time", "feedback_records", ["status", "created_at", "id"], unique=False
    )
    op.create_index("ix_feedback_time", "feedback_records", ["created_at", "id"], unique=False)
    op.create_table(
        "feedback_attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feedback_id", sa.String(length=36), nullable=True),
        sa.Column("uploader_id", sa.String(length=200), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("original_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("security_level", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state = 'attached' AND feedback_id IS NOT NULL AND expires_at IS "
            "NULL) OR (state IN ('uploading', 'staged', 'deleting') AND feedback_id"
            " IS NULL AND expires_at IS NOT NULL)",
            name=op.f("ck_feedback_attachments_feedback_attachment_binding"),
        ),
        sa.CheckConstraint(
            "state IN ('uploading', 'staged', 'attached', 'deleting')",
            name=op.f("ck_feedback_attachments_feedback_attachment_state"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('staged', 'attached') OR (byte_size > 0 AND sha256 IS NOT NULL)",
            name=op.f("ck_feedback_attachments_feedback_attachment_ready"),
        ),
        sa.CheckConstraint(
            "byte_size >= 0", name=op.f("ck_feedback_attachments_feedback_attachment_size")
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id"],
            ["feedback_records.id"],
            name=op.f("fk_feedback_attachments_feedback_id_feedback_records"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback_attachments")),
        sa.UniqueConstraint("storage_backend", "object_key", name="uq_feedback_object"),
        sa.UniqueConstraint("uploader_id", "client_request_id", name="uq_feedback_upload"),
    )
    op.create_index(
        "ix_feedback_attachment_cleanup",
        "feedback_attachments",
        ["state", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_feedback_attachment_record",
        "feedback_attachments",
        ["feedback_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "feedback_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feedback_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("actor_name", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("resulting_version", sa.Integer(), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(action = 'submitted' AND from_status IS NULL AND to_status = "
            "'processing' AND resulting_version = 1 AND content = '') OR (action = "
            "'replied' AND from_status IS NOT NULL AND from_status IN "
            "('processing', 'resolved') AND to_status = from_status AND "
            "resulting_version > 1) OR (action = 'resolved' AND from_status = "
            "'processing' AND from_status IS NOT NULL AND to_status = 'resolved' "
            "AND resulting_version > 1) OR (action = 'reopened' AND from_status = "
            "'resolved' AND from_status IS NOT NULL AND to_status = 'processing' "
            "AND resulting_version > 1)",
            name=op.f("ck_feedback_events_feedback_event_transition"),
        ),
        sa.CheckConstraint(
            "action = 'submitted' OR length(trim(content)) BETWEEN 1 AND 5000",
            name=op.f("ck_feedback_events_feedback_event_content"),
        ),
        sa.CheckConstraint(
            "resulting_version >= 1", name=op.f("ck_feedback_events_feedback_event_version")
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id"],
            ["feedback_records.id"],
            name=op.f("fk_feedback_events_feedback_id_feedback_records"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback_events")),
        sa.UniqueConstraint(
            "feedback_id", "actor_id", "client_request_id", name="uq_feedback_event_retry"
        ),
        sa.UniqueConstraint("feedback_id", "resulting_version", name="uq_feedback_event_version"),
    )
    op.create_index(
        "ix_feedback_event_time",
        "feedback_events",
        ["feedback_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_event_time", table_name="feedback_events")
    op.drop_table("feedback_events")
    op.drop_index("ix_feedback_attachment_record", table_name="feedback_attachments")
    op.drop_index("ix_feedback_attachment_cleanup", table_name="feedback_attachments")
    op.drop_table("feedback_attachments")
    op.drop_index("ix_feedback_time", table_name="feedback_records")
    op.drop_index("ix_feedback_status_time", table_name="feedback_records")
    op.drop_index("ix_feedback_owner_time", table_name="feedback_records")
    op.drop_table("feedback_records")
