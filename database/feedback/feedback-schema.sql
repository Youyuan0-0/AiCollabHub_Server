-- PostgreSQL feedback schema. Generated from implemented models.

-- Use Alembic for existing deployments; do not apply both this file and the migration.

BEGIN;

CREATE TABLE feedback_records (
	id VARCHAR(36) NOT NULL,
	submitter_id VARCHAR(200) NOT NULL,
	submitter_name VARCHAR(100) NOT NULL,
	submitter_org_id VARCHAR(200),
	feedback_type VARCHAR(32) NOT NULL,
	type_other VARCHAR(200),
	module_code VARCHAR(32) NOT NULL,
	module_other VARCHAR(200),
	title VARCHAR(60) NOT NULL,
	description TEXT NOT NULL,
	contact VARCHAR(200) NOT NULL,
	status VARCHAR(20) NOT NULL,
	version INTEGER NOT NULL,
	client_request_id VARCHAR(36) NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	resolved_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_feedback_records PRIMARY KEY (id),
	CONSTRAINT ck_feedback_records_feedback_title CHECK (length(trim(title)) BETWEEN 1 AND 60),
	CONSTRAINT ck_feedback_records_feedback_contact CHECK (length(trim(contact)) BETWEEN 1 AND 200),
	CONSTRAINT ck_feedback_records_feedback_status CHECK (status IN ('processing', 'resolved')),
	CONSTRAINT ck_feedback_records_feedback_description CHECK (length(trim(description)) BETWEEN 1 AND 5000),
	CONSTRAINT uq_feedback_submission UNIQUE (submitter_id, client_request_id),
	CONSTRAINT ck_feedback_records_feedback_resolution CHECK ((status = 'processing' AND resolved_at IS NULL) OR (status = 'resolved' AND resolved_at IS NOT NULL)),
	CONSTRAINT ck_feedback_records_feedback_version CHECK (version >= 1)
);

CREATE INDEX ix_feedback_owner_time ON feedback_records (submitter_id, created_at, id);

CREATE INDEX ix_feedback_status_time ON feedback_records (status, created_at, id);

CREATE INDEX ix_feedback_time ON feedback_records (created_at, id);

CREATE TABLE feedback_attachments (
	id VARCHAR(36) NOT NULL,
	feedback_id VARCHAR(36),
	uploader_id VARCHAR(200) NOT NULL,
	client_request_id VARCHAR(36) NOT NULL,
	original_name VARCHAR(200) NOT NULL,
	content_type VARCHAR(128) NOT NULL,
	byte_size BIGINT NOT NULL,
	sha256 VARCHAR(64),
	storage_backend VARCHAR(20) NOT NULL,
	storage_bucket VARCHAR(255) NOT NULL,
	object_key VARCHAR(512) NOT NULL,
	security_level VARCHAR(32),
	state VARCHAR(20) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_feedback_attachments PRIMARY KEY (id),
	CONSTRAINT fk_feedback_attachments_feedback_id_feedback_records FOREIGN KEY(feedback_id) REFERENCES feedback_records (id) ON DELETE RESTRICT,
	CONSTRAINT ck_feedback_attachments_feedback_attachment_ready CHECK (state NOT IN ('staged', 'attached') OR (byte_size > 0 AND sha256 IS NOT NULL)),
	CONSTRAINT ck_feedback_attachments_feedback_attachment_state CHECK (state IN ('uploading', 'staged', 'attached', 'deleting')),
	CONSTRAINT uq_feedback_upload UNIQUE (uploader_id, client_request_id),
	CONSTRAINT ck_feedback_attachments_feedback_attachment_binding CHECK ((state = 'attached' AND feedback_id IS NOT NULL AND expires_at IS NULL) OR (state IN ('uploading', 'staged', 'deleting') AND feedback_id IS NULL AND expires_at IS NOT NULL)),
	CONSTRAINT ck_feedback_attachments_feedback_attachment_size CHECK (byte_size >= 0),
	CONSTRAINT uq_feedback_object UNIQUE (storage_backend, object_key)
);

CREATE INDEX ix_feedback_attachment_cleanup ON feedback_attachments (state, expires_at);

CREATE INDEX ix_feedback_attachment_record ON feedback_attachments (feedback_id, created_at, id);

CREATE TABLE feedback_events (
	id VARCHAR(36) NOT NULL,
	feedback_id VARCHAR(36) NOT NULL,
	actor_id VARCHAR(200) NOT NULL,
	actor_name VARCHAR(100) NOT NULL,
	action VARCHAR(20) NOT NULL,
	from_status VARCHAR(20),
	to_status VARCHAR(20) NOT NULL,
	content TEXT NOT NULL,
	resulting_version INTEGER NOT NULL,
	client_request_id VARCHAR(36) NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_feedback_events PRIMARY KEY (id),
	CONSTRAINT ck_feedback_events_feedback_event_version CHECK (resulting_version >= 1),
	CONSTRAINT uq_feedback_event_retry UNIQUE (feedback_id, actor_id, client_request_id),
	CONSTRAINT fk_feedback_events_feedback_id_feedback_records FOREIGN KEY(feedback_id) REFERENCES feedback_records (id) ON DELETE RESTRICT,
	CONSTRAINT ck_feedback_events_feedback_event_transition CHECK ((action = 'submitted' AND from_status IS NULL AND to_status = 'processing' AND resulting_version = 1 AND content = '') OR (action = 'replied' AND from_status IS NOT NULL AND from_status IN ('processing', 'resolved') AND to_status = from_status AND resulting_version > 1) OR (action = 'resolved' AND from_status = 'processing' AND from_status IS NOT NULL AND to_status = 'resolved' AND resulting_version > 1) OR (action = 'reopened' AND from_status = 'resolved' AND from_status IS NOT NULL AND to_status = 'processing' AND resulting_version > 1)),
	CONSTRAINT uq_feedback_event_version UNIQUE (feedback_id, resulting_version),
	CONSTRAINT ck_feedback_events_feedback_event_content CHECK (action = 'submitted' OR length(trim(content)) BETWEEN 1 AND 5000)
);

CREATE INDEX ix_feedback_event_time ON feedback_events (feedback_id, created_at, id);

COMMIT;
