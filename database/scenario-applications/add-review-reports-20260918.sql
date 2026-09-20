-- 增量迁移：只增加应用审核报告索引，不修改已有应用和审核记录。
CREATE TABLE IF NOT EXISTS scene_review_report_file (
    id text PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    publication_id varchar(80) NOT NULL,
    publication_revision integer NOT NULL CHECK (publication_revision > 0),
    review_note_id varchar(80) REFERENCES scene_review_note(id),
    created_by_subject text NOT NULL,
    original_name text NOT NULL CHECK (length(original_name) BETWEEN 1 AND 200),
    byte_size bigint NOT NULL CHECK (byte_size > 0 AND byte_size <= 20971520),
    content_type text NOT NULL,
    bucket_name text NOT NULL,
    object_key text NOT NULL,
    preview_key text NOT NULL,
    preview_content_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bucket_name, object_key)
);
CREATE INDEX IF NOT EXISTS ix_scene_review_report_publication
    ON scene_review_report_file(publication_id, publication_revision);
CREATE INDEX IF NOT EXISTS ix_scene_review_report_note ON scene_review_report_file(review_note_id);
COMMENT ON TABLE scene_review_report_file IS
    '可选质量评测报告；文件存于 MinIO，上传暂存后与审核记录在同一事务中关联';
COMMENT ON COLUMN scene_review_report_file.review_note_id IS
    '未提交审核时为空；提交后固定归属本次审核，后续版本不覆盖原报告';
