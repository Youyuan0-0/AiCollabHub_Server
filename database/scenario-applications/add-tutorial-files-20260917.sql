-- 已有场景数据库的增量变更；只新增教程文件元信息表，不修改历史业务数据。
CREATE TABLE IF NOT EXISTS scene_tutorial_file (
    id text PRIMARY KEY,
    created_by_subject text NOT NULL,
    organization_id text NOT NULL DEFAULT '',
    original_name text NOT NULL CHECK (length(original_name) BETWEEN 1 AND 200),
    byte_size bigint NOT NULL CHECK (byte_size > 0 AND byte_size <= 104857600),
    content_type text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('document', 'video')),
    security_level text NOT NULL CHECK (security_level IN ('internal', 'public')),
    object_key text NOT NULL UNIQUE,
    preview_key text NOT NULL,
    preview_content_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE scene_tutorial_file IS '教程原文件与阅读预览的持久化索引；内容存于受控文件服务，访问需检查应用或发布申请权限';
