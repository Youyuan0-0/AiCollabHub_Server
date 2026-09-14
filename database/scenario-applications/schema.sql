-- 场景应用数据库设计 V1，2026-09-14
-- 适用：PostgreSQL 16 / 17；UTF-8；沿用当前 SQLAlchemy / Alembic 框架。
-- 前提：协同平台管理本地目录、发布审批及个人互动，供应方提供工具链和运行服务。
-- 本文件是首次建表脚本，不是已接入代码的 Alembic 迁移；设计阶段不对现有库执行。
-- 在选定开发主库首次执行：psql -X -v ON_ERROR_STOP=1 -d <数据库名> -f schema.sql
-- 已有同名表时整笔失败，避免 IF NOT EXISTS 掩盖结构不一致；不含 DROP/TRUNCATE。
-- 不创建用户/组织/项目表，不修改 auth_session 等现有认证表。
-- *_subject 存可信 IAM subject（最多 200 字），不能关联会过期清理的 auth_session。
-- 组织/项目编号与其他资源编号暂作逻辑关联，正式公共目录确定后再补相应外键。
-- 本地业务 ID 由后端生成，兼容 app-/version-/publication- 加 UUID 的现有格式。
-- 时间均用 timestamptz，展示时转北京时间；updated_at 与 revision 由仓储显式更新。
-- JSONB 用于结构不固定且整体读取的内容；主关系、状态、筛选字段使用普通列。

BEGIN;
SET LOCAL search_path = public;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 01 分类字典：前端名称仍显示中文，后端以稳定编号关联。
CREATE TABLE scene_category (
    id varchar(80) PRIMARY KEY,                       -- 分类编号
    name varchar(80) NOT NULL UNIQUE,                 -- 分类名称
    sort_order integer NOT NULL DEFAULT 0,            -- 分类导航顺序，越小越靠前
    enabled boolean NOT NULL DEFAULT true,            -- 是否允许新提交选择
    created_at timestamptz NOT NULL DEFAULT now()      -- 创建时间
);
COMMENT ON TABLE scene_category IS '场景应用分类字典';

-- 02 应用主表：从第一份草稿开始就有主记录；审核通过前不出现在公开目录。
CREATE TABLE scene_application (
    id varchar(80) PRIMARY KEY,                       -- 本地应用编号，对应 applicationId
    created_by_subject varchar(200) NOT NULL,         -- 原始发布者的可信用户编号
    created_by_name varchar(200) NOT NULL DEFAULT '', -- 原始发布者姓名快照
    organization_id varchar(200),                     -- 权限归属组织；未知为 NULL，不填演示组织
    current_version_id varchar(80),                  -- 当前上架版本；首次通过审核前为空
    visibility_scope varchar(20) NOT NULL DEFAULT 'all', -- all/organizations/members/projects
    visibility_revision bigint NOT NULL DEFAULT 1,   -- 开放范围独立修订号
    revision bigint NOT NULL DEFAULT 1,              -- 应用主记录乐观锁修订号
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scene_application_scope CHECK (
        visibility_scope IN ('all', 'organizations', 'members', 'projects')
    ),
    CONSTRAINT ck_scene_application_revision CHECK (revision > 0 AND visibility_revision > 0),
    CONSTRAINT ck_scene_application_subject CHECK (btrim(created_by_subject) <> ''),
    CONSTRAINT ck_scene_application_organization CHECK (
        organization_id IS NULL OR btrim(organization_id) <> ''
    ),
    CONSTRAINT uq_scene_application_scope UNIQUE (id, visibility_scope)
);
CREATE INDEX ix_scene_application_owner ON scene_application (created_by_subject, updated_at DESC);
CREATE INDEX ix_scene_application_org ON scene_application (organization_id, updated_at DESC);
COMMENT ON TABLE scene_application IS '应用稳定身份、归属、当前上架版本和开放范围';

-- 03 供应方连接：区分系统、环境、租户；密钥仅引用后端配置，不存明文令牌。
CREATE TABLE scene_provider_connection (
    id varchar(80) PRIMARY KEY,
    provider_code varchar(80) NOT NULL,               -- 供应方/系统代号，如 agent-dev
    environment_code varchar(80) NOT NULL,            -- dev/test/prod 等明确环境
    tenant_code varchar(200) NOT NULL DEFAULT '',     -- 供应方租户域；无租户时使用空串
    name varchar(200) NOT NULL,
    api_base_url varchar(2000),                       -- 对方实际 API 基地址；尚未取得可为空
    credential_ref varchar(200),                      -- 后端环境变量/密钥服务引用名称
    enabled boolean NOT NULL DEFAULT false,          -- 接入确认前默认关闭
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_provider_environment UNIQUE (provider_code, environment_code, tenant_code),
    CONSTRAINT ck_scene_provider_names CHECK (
        btrim(provider_code) <> '' AND btrim(environment_code) <> ''
    )
);
COMMENT ON TABLE scene_provider_connection IS '供应方环境登记；地址与凭证由后端管理，不下发给浏览器';

-- 04 外部应用绑定：外部编号可能跨环境/工作空间重复，不能单独设全局唯一。
CREATE TABLE scene_external_binding (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    connection_id varchar(80) NOT NULL REFERENCES scene_provider_connection(id),
    workspace_code varchar(200) NOT NULL,             -- 对方 workspaceCode，不等于平台组织编号
    external_application_code varchar(200) NOT NULL,  -- 对方 agentCode / 资源编号
    external_biz_type varchar(100),                   -- 对方 bizType，仅是技术类型
    created_by_subject varchar(200) NOT NULL,
    last_synced_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_binding_source UNIQUE (
        connection_id, workspace_code, external_application_code
    ),
    CONSTRAINT uq_scene_binding_application UNIQUE (application_id, id),
    CONSTRAINT ck_scene_binding_codes CHECK (
        btrim(workspace_code) <> '' AND btrim(external_application_code) <> ''
    )
);
COMMENT ON TABLE scene_external_binding IS '本地应用与供应方应用的对应关系';

-- 05 版本资料：保存草稿和每个已发布版本，旧版本资料不被新版覆盖。
CREATE TABLE scene_application_version (
    id varchar(80) PRIMARY KEY,                       -- 本地版本 ID，对应 versionId
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    version_label varchar(80) NOT NULL DEFAULT '',    -- 页面版本名，如 v1.0；草稿可留空
    name varchar(200) NOT NULL DEFAULT '',
    english_name varchar(200) NOT NULL DEFAULT '',
    summary varchar(1000) NOT NULL DEFAULT '',
    description_markdown text NOT NULL DEFAULT '',
    category_id varchar(80) REFERENCES scene_category(id), -- 草稿可为空，发布时必须有效
    owner_display_name varchar(200) NOT NULL DEFAULT '', -- 表单 owner，所属单位展示文字
    contact varchar(200) NOT NULL DEFAULT '',
    tags varchar(80)[] NOT NULL DEFAULT '{}',
    capabilities varchar(80)[] NOT NULL DEFAULT '{}',
    base_model varchar(200) NOT NULL DEFAULT '',
    tutorials jsonb NOT NULL DEFAULT '[]',            -- [{title,kind:document|video,url}]
    config_options jsonb NOT NULL DEFAULT '[]',       -- [{name,value,description}]
    usage_limits text NOT NULL DEFAULT '',
    technical_stack varchar(500) NOT NULL DEFAULT '',
    deployment varchar(200) NOT NULL DEFAULT '',
    trigger_type varchar(200) NOT NULL DEFAULT '',
    communication_protocol varchar(200) NOT NULL DEFAULT '',
    input_schema jsonb,                              -- JSON Schema 对象；表单空串映射为 NULL
    output_schema jsonb,                             -- JSON Schema 对象；读取时转回 API 所需字符串
    version_notes text NOT NULL DEFAULT '',
    security_level varchar(20) NOT NULL DEFAULT 'internal', -- internal/public/confidential
    creation_method varchar(30) NOT NULL DEFAULT 'manual', -- manual/toolchain_import
    binding_id varchar(80),                          -- 手工创建且无外部来源时为空
    external_version_code varchar(200),              -- agentVersion：调用使用的编码，必须按字符串保存
    external_version_name varchar(200),              -- versionName：对方的显示名称
    external_status varchar(100),                    -- agentStatus：不替代本地审批状态
    external_created_at timestamptz,
    external_updated_at timestamptz,
    source_synced_at timestamptz,
    source_snapshot jsonb,                           -- 脱敏后的来源字段快照，不保存凭证
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,                         -- 首次审核通过的时间；为空表示未发布
    CONSTRAINT uq_scene_version_application UNIQUE (application_id, id),
    CONSTRAINT fk_scene_version_binding FOREIGN KEY (application_id, binding_id)
        REFERENCES scene_external_binding(application_id, id),
    CONSTRAINT ck_scene_version_source CHECK (
        (binding_id IS NULL AND external_version_code IS NULL AND creation_method = 'manual')
        OR (binding_id IS NOT NULL AND external_version_code IS NOT NULL
            AND btrim(external_version_code) <> '')
    ),
    CONSTRAINT ck_scene_version_method CHECK (creation_method IN ('manual', 'toolchain_import')),
    CONSTRAINT ck_scene_version_security CHECK (security_level IN ('internal','public','confidential')),
    CONSTRAINT ck_scene_version_published_fields CHECK (
        released_at IS NULL OR (
            btrim(version_label) <> '' AND btrim(name) <> '' AND btrim(summary) <> ''
            AND btrim(owner_display_name) <> '' AND category_id IS NOT NULL
            AND security_level <> 'confidential'
        )
    ),
    CONSTRAINT ck_scene_version_labels CHECK (
        cardinality(tags) <= 20 AND cardinality(capabilities) <= 20
        AND array_position(tags, NULL) IS NULL AND array_position(capabilities, NULL) IS NULL
    ),
    CONSTRAINT ck_scene_version_tutorials CHECK (
        jsonb_typeof(tutorials) = 'array' AND jsonb_array_length(tutorials) <= 10
    ),
    CONSTRAINT ck_scene_version_options CHECK (
        jsonb_typeof(config_options) = 'array' AND jsonb_array_length(config_options) <= 20
    ),
    CONSTRAINT ck_scene_version_schema CHECK (
        (input_schema IS NULL OR jsonb_typeof(input_schema) = 'object')
        AND (output_schema IS NULL OR jsonb_typeof(output_schema) = 'object')
    ),
    CONSTRAINT ck_scene_version_source_snapshot CHECK (
        source_snapshot IS NULL OR jsonb_typeof(source_snapshot) = 'object'
    ),
    CONSTRAINT ck_scene_version_text_lengths CHECK (
        char_length(description_markdown) <= 30000 AND char_length(usage_limits) <= 5000
        AND char_length(version_notes) <= 5000
    )
);
CREATE UNIQUE INDEX uq_scene_version_released_label
    ON scene_application_version(application_id, lower(btrim(version_label)))
    WHERE released_at IS NOT NULL;
-- 同一外部版本只“导入”为一份申请；手工从该版本另建本地新版可复用外部运行版本。
CREATE UNIQUE INDEX uq_scene_version_import
    ON scene_application_version(binding_id, external_version_code)
    WHERE creation_method = 'toolchain_import';
CREATE INDEX ix_scene_version_category ON scene_application_version(category_id);
CREATE INDEX ix_scene_version_tags ON scene_application_version USING gin(tags);
CREATE INDEX ix_scene_version_binding ON scene_application_version(binding_id);
ALTER TABLE scene_application ADD CONSTRAINT fk_scene_application_current_version
    FOREIGN KEY (id, current_version_id) REFERENCES scene_application_version(application_id, id)
    DEFERRABLE INITIALLY DEFERRED;
COMMENT ON TABLE scene_application_version IS '每个应用的版本资料；released_at 非空后资料冻结';

-- 06 四类依赖：不复制对方模型/数据集/知识库/工具的底层数据。
CREATE TABLE scene_version_dependency (
    version_id varchar(80) NOT NULL REFERENCES scene_application_version(id),
    resource_kind varchar(20) NOT NULL CHECK (resource_kind IN ('model','dataset','knowledge','tool')),
    resource_id varchar(200) NOT NULL CHECK (btrim(resource_id) <> ''), -- 平台资源目录返回的 ID
    resource_version_code varchar(200),               -- 预留版本锁定；现有 API 只关联资源 ID
    resource_name_snapshot varchar(200),             -- 可选名称快照；实时权限由关联模块校验
    sort_order integer NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    PRIMARY KEY (version_id, resource_kind, resource_id)
);
CREATE INDEX ix_scene_dependency_reverse ON scene_version_dependency(resource_kind, resource_id);
COMMENT ON TABLE scene_version_dependency IS '应用版本关联的模型、数据集、知识库和 AI 工具';

-- 07 发布申请：同一申请驳回/补材料后继续修改、重提；一个应用只保留一个未完成申请。
CREATE TABLE scene_publication (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    version_id varchar(80) NOT NULL UNIQUE,
    base_version_id varchar(80),                      -- 创建新版时依据的当前已发布版本
    status varchar(20) NOT NULL DEFAULT 'draft',
    priority varchar(10) NOT NULL DEFAULT 'normal',
    created_by_subject varchar(200) NOT NULL,         -- 沿用原始发布者归属
    created_by_name varchar(200) NOT NULL DEFAULT '',
    last_edited_by_subject varchar(200),              -- 实际编辑人，管理员代改不更换所有者
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    form_revision bigint CHECK (form_revision > 0),
    submission_rules jsonb,                          -- 提交时必填字段/提示等完整规则快照
    submitted_at timestamptz,                        -- 最近一次提交时间
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_publication_application UNIQUE (application_id, id),
    CONSTRAINT fk_scene_publication_version FOREIGN KEY (application_id, version_id)
        REFERENCES scene_application_version(application_id, id),
    CONSTRAINT fk_scene_publication_base FOREIGN KEY (application_id, base_version_id)
        REFERENCES scene_application_version(application_id, id),
    CONSTRAINT ck_scene_publication_base CHECK (base_version_id IS NULL OR base_version_id <> version_id),
    CONSTRAINT ck_scene_publication_status CHECK (
        status IN ('draft','pending','approved','rejected','needs_changes')
    ),
    CONSTRAINT ck_scene_publication_priority CHECK (priority IN ('normal','high','urgent')),
    CONSTRAINT ck_scene_publication_rules CHECK (
        submission_rules IS NULL OR jsonb_typeof(submission_rules) = 'object'
    ),
    CONSTRAINT ck_scene_publication_submitted CHECK (
        status = 'draft' OR (submitted_at IS NOT NULL AND form_revision IS NOT NULL
            AND submission_rules IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_scene_publication_open
    ON scene_publication(application_id) WHERE status <> 'approved';
CREATE INDEX ix_scene_publication_queue ON scene_publication(status, submitted_at DESC);
CREATE INDEX ix_scene_publication_priority ON scene_publication(
    status, (CASE priority WHEN 'urgent' THEN 2 WHEN 'high' THEN 1 ELSE 0 END) DESC,
    submitted_at DESC
);
CREATE INDEX ix_scene_publication_owner ON scene_publication(created_by_subject, updated_at DESC);
COMMENT ON TABLE scene_publication IS '草稿、提交及审核状态；一份申请对应一个版本，可多次提交审核';

-- 08 审核历史：每次审核新增一行，保存当时资料、规则、人工三项评分。
CREATE TABLE scene_review (
    id varchar(80) PRIMARY KEY,
    publication_id varchar(80) NOT NULL REFERENCES scene_publication(id),
    publication_revision bigint NOT NULL CHECK (publication_revision > 0), -- 审核前申请修订号
    decision varchar(20) NOT NULL CHECK (decision IN ('approved','rejected','needs_changes')),
    reason varchar(2000) NOT NULL DEFAULT '',
    reviewer_subject varchar(200) NOT NULL,
    reviewer_name varchar(200) NOT NULL DEFAULT '',
    completeness_score smallint CHECK (completeness_score BETWEEN 1 AND 5),
    usability_score smallint CHECK (usability_score BETWEEN 1 AND 5),
    security_score smallint CHECK (security_score BETWEEN 1 AND 5),
    minimum_score smallint NOT NULL CHECK (minimum_score BETWEEN 1 AND 5), -- 当时通过阈值
    data_snapshot jsonb NOT NULL CHECK (jsonb_typeof(data_snapshot) = 'object'), -- 完整 ApplicationDetails
    submission_rules_snapshot jsonb NOT NULL CHECK (jsonb_typeof(submission_rules_snapshot) = 'object'),
    policy_snapshot jsonb NOT NULL CHECK (jsonb_typeof(policy_snapshot) = 'object'), -- 审核时规则
    quality_report_snapshot jsonb CHECK (jsonb_typeof(quality_report_snapshot) = 'object'),
    submitted_at timestamptz NOT NULL,
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_review_revision UNIQUE (publication_id, publication_revision),
    CONSTRAINT ck_scene_review_scores CHECK (
        num_nonnulls(completeness_score, usability_score, security_score) IN (0,3)
    ),
    CONSTRAINT ck_scene_review_approval CHECK (
        decision <> 'approved' OR (
            completeness_score IS NOT NULL AND usability_score IS NOT NULL AND security_score IS NOT NULL
            AND completeness_score >= minimum_score AND usability_score >= minimum_score
            AND security_score >= minimum_score
        )
    ),
    CONSTRAINT ck_scene_review_reason CHECK (decision = 'approved' OR btrim(reason) <> '')
);
CREATE INDEX ix_scene_review_history ON scene_review(reviewer_subject, reviewed_at DESC);
CREATE INDEX ix_scene_review_decision ON scene_review(decision, reviewed_at DESC);
COMMENT ON TABLE scene_review IS '不可覆盖的审核历史及快照；本地质量检查不是供应方 AI 效果评测';

-- 09 可见对象：all 不写目标；其他范围写组织/用户/项目编号，一种范围可以多个目标。
CREATE TABLE scene_visibility_target (
    application_id varchar(80) NOT NULL,
    scope varchar(20) NOT NULL CHECK (scope IN ('organizations','members','projects')),
    target_id varchar(200) NOT NULL CHECK (btrim(target_id) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, scope, target_id),
    CONSTRAINT fk_scene_visibility_scope FOREIGN KEY (application_id, scope)
        REFERENCES scene_application(id, visibility_scope) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_scene_visibility_lookup ON scene_visibility_target(scope, target_id, application_id);
COMMENT ON TABLE scene_visibility_target IS '应用开放的组织、成员或项目；实际鉴权由可信 IAM 归属与后端规则完成';

-- 10 个人互动：一人一应用一行；收藏/订阅/评分可同时存在，避免拆成三套重复接口。
CREATE TABLE scene_interaction (
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    actor_subject varchar(200) NOT NULL,
    favorite boolean NOT NULL DEFAULT false,
    subscribed boolean NOT NULL DEFAULT false,
    rating smallint CHECK (rating BETWEEN 1 AND 5),
    comment varchar(1000) NOT NULL DEFAULT '',         -- 与该用户评分一起更新，不是独立讨论区
    favorited_at timestamptz,
    subscribed_at timestamptz,                       -- 独立记录，之后评分不应改写订阅时间
    rated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, actor_subject),
    CONSTRAINT ck_scene_interaction_favorite CHECK (favorite = (favorited_at IS NOT NULL)),
    CONSTRAINT ck_scene_interaction_subscription CHECK (subscribed = (subscribed_at IS NOT NULL)),
    CONSTRAINT ck_scene_interaction_rating CHECK (
        (rating IS NULL AND rated_at IS NULL AND comment = '')
        OR (rating IS NOT NULL AND rated_at IS NOT NULL)
    )
);
CREATE INDEX ix_scene_interaction_favorites ON scene_interaction(actor_subject, favorited_at DESC)
    WHERE favorite;
CREATE INDEX ix_scene_interaction_subscriptions ON scene_interaction(actor_subject, subscribed_at DESC)
    WHERE subscribed;
CREATE INDEX ix_scene_interaction_subscribers ON scene_interaction(application_id, actor_subject)
    WHERE subscribed;
COMMENT ON TABLE scene_interaction IS '每个用户对每个应用的收藏、订阅、评分和评价';

-- 11 运行记录：请求 ID 防重复；模拟数据与真实调用明确分开。
CREATE TABLE scene_run (
    id varchar(80) PRIMARY KEY,                       -- 新建稳定记录 ID，API 可继续组合原有显示 ID
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    version_id varchar(80) NOT NULL,
    actor_subject varchar(200) NOT NULL,
    request_id varchar(100) NOT NULL CHECK (char_length(request_id) BETWEEN 8 AND 100),
    request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    application_name_snapshot varchar(200) NOT NULL,
    version_label_snapshot varchar(80) NOT NULL,
    mode varchar(20) NOT NULL CHECK (mode IN ('simulation','live')), -- 不设置默认，必须显式指定
    status varchar(20) NOT NULL DEFAULT 'running',
    input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload) = 'object'),
    options jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(options) = 'object'),
    output_payload jsonb,                            -- 输出对象/数组/文本，按实际接口适配
    error_code varchar(100),
    error_message text NOT NULL DEFAULT '',          -- 脱敏后的可展示错误
    external_request_id varchar(300),
    external_session_id varchar(300),                -- 预留对方运行会话编号
    duration_ms bigint CHECK (duration_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    deadline_at timestamptz,                         -- 预留异步任务恢复/超时扫描
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_run_request UNIQUE (actor_subject, application_id, request_id),
    CONSTRAINT fk_scene_run_version FOREIGN KEY (application_id, version_id)
        REFERENCES scene_application_version(application_id, id),
    CONSTRAINT ck_scene_run_status CHECK (
        status IN ('queued','running','succeeded','failed','timed_out','unknown')
    ),
    CONSTRAINT ck_scene_run_completion CHECK (
        (status IN ('succeeded','failed','timed_out') AND completed_at IS NOT NULL)
        OR (status IN ('queued','running','unknown') AND completed_at IS NULL)
    ),
    CONSTRAINT ck_scene_run_time CHECK (completed_at IS NULL OR completed_at >= created_at)
);
CREATE INDEX ix_scene_run_personal ON scene_run(actor_subject, created_at DESC);
CREATE INDEX ix_scene_run_metrics ON scene_run(application_id, mode, status, created_at DESC);
CREATE INDEX ix_scene_run_version ON scene_run(version_id);
CREATE INDEX ix_scene_run_recovery ON scene_run(deadline_at)
    WHERE status IN ('queued','running','unknown');
COMMENT ON TABLE scene_run IS '体验调用与历史；unknown 表示外部结果待核对，不等同确定失败';
COMMENT ON COLUMN scene_run.request_fingerprint IS
    '规范化输入、配置、运行模式及模拟开关的 SHA-256；相同请求 ID 不同内容应返回 409';

-- 12 下载记录：当前下载的是应用资料 JSON，保存同一次请求返回的脱敏快照。
CREATE TABLE scene_download (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    version_id varchar(80) NOT NULL,
    actor_subject varchar(200) NOT NULL,
    request_id varchar(100) NOT NULL CHECK (char_length(request_id) BETWEEN 8 AND 100),
    filename varchar(300) NOT NULL,
    content_snapshot jsonb NOT NULL CHECK (jsonb_typeof(content_snapshot) = 'object'),
    downloaded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_download_request UNIQUE (actor_subject, application_id, request_id),
    CONSTRAINT fk_scene_download_version FOREIGN KEY (application_id, version_id)
        REFERENCES scene_application_version(application_id, id)
);
CREATE INDEX ix_scene_download_app ON scene_download(application_id, downloaded_at DESC);
CREATE INDEX ix_scene_download_actor ON scene_download(actor_subject, downloaded_at DESC);
CREATE INDEX ix_scene_download_version ON scene_download(version_id);
COMMENT ON TABLE scene_download IS '应用资料 JSON 下载记录，非模型文件或可执行程序包';

-- 13 工具链同步：即使未读到外部应用/绑定失败，也保留同步请求与失败原因。
CREATE TABLE scene_sync_job (
    id varchar(80) PRIMARY KEY,
    connection_id varchar(80) NOT NULL REFERENCES scene_provider_connection(id),
    workspace_code varchar(200) NOT NULL,
    external_application_code varchar(200) NOT NULL,
    external_version_code varchar(200) NOT NULL,
    actor_subject varchar(200) NOT NULL,
    organization_id varchar(200),
    request_id varchar(100) NOT NULL CHECK (char_length(request_id) BETWEEN 8 AND 100),
    request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    application_id varchar(80),                       -- 成功确定本地归属后填写
    publication_id varchar(80),                       -- 新导入或重复版本沿用的申请
    status varchar(20) NOT NULL DEFAULT 'running',
    message text NOT NULL DEFAULT '',
    field_mapping jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(field_mapping) = 'array'),
    source_snapshot jsonb CHECK (jsonb_typeof(source_snapshot) = 'object'),
    retry_of_id varchar(80) REFERENCES scene_sync_job(id), -- 重试创建新请求，不覆盖原始失败记录
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT uq_scene_sync_request UNIQUE (actor_subject, request_id),
    CONSTRAINT fk_scene_sync_application FOREIGN KEY (application_id) REFERENCES scene_application(id),
    CONSTRAINT fk_scene_sync_publication FOREIGN KEY (application_id, publication_id)
        REFERENCES scene_publication(application_id, id) MATCH FULL,
    CONSTRAINT ck_scene_sync_status CHECK (status IN ('running','succeeded','unchanged','failed')),
    CONSTRAINT ck_scene_sync_result CHECK (
        status NOT IN ('succeeded','unchanged') OR publication_id IS NOT NULL
    ),
    CONSTRAINT ck_scene_sync_completion CHECK ((status = 'running') = (completed_at IS NULL)),
    CONSTRAINT ck_scene_sync_retry CHECK (retry_of_id IS NULL OR retry_of_id <> id),
    CONSTRAINT ck_scene_sync_codes CHECK (
        btrim(workspace_code) <> '' AND btrim(external_application_code) <> ''
        AND btrim(external_version_code) <> ''
    )
);
CREATE INDEX ix_scene_sync_owner ON scene_sync_job(actor_subject, created_at DESC);
CREATE INDEX ix_scene_sync_org ON scene_sync_job(organization_id, created_at DESC);
CREATE INDEX ix_scene_sync_source ON scene_sync_job(
    connection_id, workspace_code, external_application_code, external_version_code
);
CREATE INDEX ix_scene_sync_publication ON scene_sync_job(application_id, publication_id);
CREATE INDEX ix_scene_sync_retry ON scene_sync_job(retry_of_id);
COMMENT ON TABLE scene_sync_job IS '工具链导入、字段映射、重复同步与失败重试记录';

-- 14 通知事件：审核结果、订阅新版、运行失败报警；只包含站内消息。
CREATE TABLE scene_notification (
    id varchar(80) PRIMARY KEY,
    event_key varchar(500) NOT NULL UNIQUE,           -- 事件去重键，包含版本/审核/接收对象等编号
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    kind varchar(20) NOT NULL CHECK (kind IN ('review','new_version','run_failure')),
    recipient_kind varchar(10) NOT NULL CHECK (recipient_kind IN ('user','role')),
    recipient_id varchar(200) NOT NULL,               -- 用户 subject；角色通知用 admin
    title varchar(1000) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scene_notification_recipient CHECK (
        btrim(recipient_id) <> '' AND (recipient_kind <> 'role' OR recipient_id = 'admin')
    )
);
CREATE INDEX ix_scene_notification_recipient
    ON scene_notification(recipient_kind, recipient_id, created_at DESC);
CREATE INDEX ix_scene_notification_application ON scene_notification(application_id);
COMMENT ON TABLE scene_notification IS '站内消息；系统管理员广播按角色保存事件，个人阅读状态另存';

-- 15 阅读回执：同一条管理员广播由各管理员分别标记已读。
CREATE TABLE scene_notification_receipt (
    notification_id varchar(80) NOT NULL REFERENCES scene_notification(id),
    actor_subject varchar(200) NOT NULL,
    read_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (notification_id, actor_subject)
);
CREATE INDEX ix_scene_notification_receipt_actor ON scene_notification_receipt(actor_subject);
COMMENT ON TABLE scene_notification_receipt IS '用户个人阅读状态；无回执即未读，后端校验消息接收权限';

-- 16 表单/管理列配置：结构与当前 configuration.py 保持一致。
CREATE TABLE scene_form_configuration (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1), -- 单例
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    required_fields jsonb NOT NULL CHECK (jsonb_typeof(required_fields) = 'array'),
    field_hints jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(field_hints) = 'object'),
    management_columns jsonb NOT NULL CHECK (jsonb_typeof(management_columns) = 'array'),
    updated_by_subject varchar(200),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scene_form_core_fields CHECK (
        required_fields @> '["name","version","category","owner","summary"]'::jsonb
    ),
    CONSTRAINT ck_scene_form_columns CHECK (jsonb_array_length(management_columns) > 0)
);
COMMENT ON TABLE scene_form_configuration IS '发布表单必填字段、提示及管理列表列顺序；字段合法性沿用后端校验';

-- 17 业务规则配置：小型有序数组整体读写；保存前校验应用/分类存在与调用者权限。
CREATE TABLE scene_workflow_policy (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    toolchain_url varchar(2000) NOT NULL DEFAULT '',   -- 浏览器开发入口，非后台调用基地址
    announcement varchar(1000) NOT NULL DEFAULT '',
    recommended_ids varchar(80)[] NOT NULL DEFAULT '{}', -- 推荐应用按数组顺序显示，最多 12 个
    hot_category_ids varchar(80)[] NOT NULL DEFAULT '{}', -- 分类编号，读取时还原当前 API 的中文名称
    default_sort varchar(30) NOT NULL DEFAULT 'recommended',
    page_size smallint NOT NULL DEFAULT 12 CHECK (page_size IN (6,12,24)),
    sensitive_terms varchar(80)[] NOT NULL DEFAULT '{}', -- 正式词库由管理员配置，不植入演示词
    minimum_quality_score smallint NOT NULL DEFAULT 3 CHECK (minimum_quality_score BETWEEN 1 AND 5),
    notifications_enabled boolean NOT NULL DEFAULT true,
    notification_template varchar(300) NOT NULL DEFAULT '{name}：{status}',
    failure_threshold smallint NOT NULL DEFAULT 3 CHECK (failure_threshold BETWEEN 1 AND 100),
    updated_by_subject varchar(200),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scene_policy_sort CHECK (
        default_sort IN ('recommended','recent_download','downloads','favorites')
    ),
    CONSTRAINT ck_scene_policy_arrays CHECK (
        cardinality(recommended_ids) <= 12 AND cardinality(hot_category_ids) <= 5
        AND cardinality(sensitive_terms) <= 100 AND array_position(recommended_ids, NULL) IS NULL
        AND array_position(hot_category_ids, NULL) IS NULL AND array_position(sensitive_terms, NULL) IS NULL
    )
);
COMMENT ON TABLE scene_workflow_policy IS '目录展示、发布规则、通知模板及开发入口；有序小数组由业务层校验引用';

-- 数据完整性：已发布资料与审核历史不可直接覆盖。
-- 这些触发器不实现审核流程；状态切换、权限、通知写入仍必须放在仓储事务中。
CREATE FUNCTION scene_reject_review_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '审核历史只能新增，不能修改或删除' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER scene_review_append_only BEFORE UPDATE OR DELETE ON scene_review
    FOR EACH ROW EXECUTE FUNCTION scene_reject_review_mutation();

CREATE FUNCTION scene_freeze_released_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.released_at IS NOT NULL THEN
        RAISE EXCEPTION '已发布版本不可覆盖，请创建新版本' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scene_version_freeze BEFORE UPDATE OR DELETE ON scene_application_version
    FOR EACH ROW EXECUTE FUNCTION scene_freeze_released_version();

CREATE FUNCTION scene_freeze_released_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    old_id varchar(80);
    new_id varchar(80);
    version_row record;
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN old_id := OLD.version_id; END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN new_id := NEW.version_id; END IF;
    -- 按稳定顺序锁住关联版本，避免“检查未发布后，另一事务已发布”的并发窗口。
    FOR version_row IN SELECT id, released_at FROM scene_application_version
        WHERE id IN (old_id, new_id) ORDER BY id FOR UPDATE
    LOOP
        IF version_row.released_at IS NOT NULL THEN
            RAISE EXCEPTION '已发布版本的依赖不可修改' USING ERRCODE = '23514';
        END IF;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scene_dependency_freeze BEFORE INSERT OR UPDATE OR DELETE ON scene_version_dependency
    FOR EACH ROW EXECUTE FUNCTION scene_freeze_released_dependency();

-- 在提交时检查“当前版本已发布”和“非全部开放至少有一个目标”。
CREATE FUNCTION scene_check_application_links() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_application_id varchar(80);
    app_row record;
BEGIN
    IF TG_TABLE_NAME = 'scene_application' THEN
        target_application_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target_application_id := OLD.application_id;
    ELSE
        target_application_id := NEW.application_id;
    END IF;
    SELECT * INTO app_row FROM scene_application WHERE id = target_application_id FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF app_row.current_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM scene_application_version
        WHERE id = app_row.current_version_id AND application_id = app_row.id AND released_at IS NOT NULL
    ) THEN
        RAISE EXCEPTION '当前上架版本必须属于本应用且已经发布' USING ERRCODE = '23514';
    END IF;
    IF app_row.visibility_scope <> 'all' AND NOT EXISTS (
        SELECT 1 FROM scene_visibility_target
        WHERE application_id = app_row.id AND scope = app_row.visibility_scope
    ) THEN
        RAISE EXCEPTION '指定开放范围时至少需要一个有效目标' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER scene_application_links AFTER INSERT OR UPDATE ON scene_application
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION scene_check_application_links();
CREATE CONSTRAINT TRIGGER scene_visibility_links AFTER INSERT OR DELETE ON scene_visibility_target
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION scene_check_application_links();
-- 范围目标的更改按“删除旧行 + 新增行”进行，避免 UPDATE 移走另一应用的最后一个目标。
CREATE FUNCTION scene_reject_target_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '可见对象请在同一事务中删除旧项再新增' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER scene_visibility_no_update BEFORE UPDATE ON scene_visibility_target
    FOR EACH ROW EXECUTE FUNCTION scene_reject_target_update();

-- 最小初始化：只有真实字典和空配置，不写假账号、假应用或模拟统计。
INSERT INTO scene_category(id, name, sort_order) VALUES
    ('document', '文档创作', 10), ('meeting', '会议办公', 20), ('data', '数据处理', 30),
    ('translation', '翻译沟通', 40), ('legal', '法律合规', 50);
INSERT INTO scene_form_configuration(required_fields, management_columns) VALUES
    ('["name","version","category","owner","summary"]',
     '["category","owner","version","usage","updatedAt"]');
INSERT INTO scene_workflow_policy(id) VALUES (1);

-- 视图 1：调用统计按 simulation/live 分组，不把演示次数混进真实业务指标。
CREATE VIEW scene_application_run_metrics AS
SELECT application_id, mode,
    count(*) AS request_count,
    count(*) FILTER (WHERE status = 'succeeded') AS success_count,
    count(*) FILTER (WHERE status IN ('failed','timed_out')) AS failure_count,
    count(*) FILTER (WHERE status IN ('queued','running','unknown')) AS unresolved_count,
    round(avg(duration_ms) FILTER (WHERE status IN ('succeeded','failed','timed_out')), 2)
        AS average_duration_ms,
    round(100.0 * count(*) FILTER (WHERE status = 'succeeded')
        / NULLIF(count(*) FILTER (WHERE status IN ('succeeded','failed','timed_out')), 0), 2)
        AS success_rate_percent
FROM scene_run GROUP BY application_id, mode;
COMMENT ON VIEW scene_application_run_metrics IS '按模拟/真实模式分别汇总调用；无完成样本时成功率和耗时为空';

-- 视图 2：每个来源先分组再关联，防止收藏×下载×运行笛卡尔积导致数量重复。
CREATE VIEW scene_application_metrics AS
SELECT a.id AS application_id,
    coalesce(i.favorite_count, 0) AS favorite_count,
    coalesce(i.subscription_count, 0) AS subscription_count,
    coalesce(i.rating_count, 0) AS rating_count,
    i.rating_average,
    coalesce(d.download_count, 0) AS download_count,
    d.last_downloaded_at,
    coalesce(live.success_count, 0) AS live_usage_count,
    coalesce(simulation.success_count, 0) AS simulation_usage_count
FROM scene_application a
LEFT JOIN (
    SELECT application_id, count(*) FILTER (WHERE favorite) AS favorite_count,
        count(*) FILTER (WHERE subscribed) AS subscription_count,
        count(rating) AS rating_count, round(avg(rating), 2) AS rating_average
    FROM scene_interaction GROUP BY application_id
) i ON i.application_id = a.id
LEFT JOIN (
    SELECT application_id, count(*) AS download_count, max(downloaded_at) AS last_downloaded_at
    FROM scene_download GROUP BY application_id
) d ON d.application_id = a.id
LEFT JOIN scene_application_run_metrics live ON live.application_id = a.id AND live.mode = 'live'
LEFT JOIN scene_application_run_metrics simulation
    ON simulation.application_id = a.id AND simulation.mode = 'simulation';
COMMENT ON VIEW scene_application_metrics IS '从明细推导收藏/订阅/评分/下载/使用量；没有预置业务计数';

COMMIT;

-- ================= 常用 SQL 示例（全在注释中，不随建表执行） =================
-- 示例中的 :xxx 是 SQLAlchemy 绑定参数；传参不能用字符串拼接。
-- 用户权限须在分页前加入 WHERE；管理员角色和组织归属来自服务端验证身份。
--
-- A. 目录查询、分类、关键词、最近下载排序。LIMIT/OFFSET 前完成权限筛选。
-- SELECT a.id, v.id AS version_id, v.name, v.summary, v.version_label, c.name AS category,
--        m.favorite_count, m.download_count, m.rating_average, m.live_usage_count
-- FROM scene_application a
-- JOIN scene_application_version v ON v.id = a.current_version_id AND v.application_id = a.id
-- JOIN scene_category c ON c.id = v.category_id
-- JOIN scene_application_metrics m ON m.application_id = a.id
-- WHERE v.released_at IS NOT NULL
--   AND (:category_id IS NULL OR v.category_id = :category_id)
--   AND (strpos(lower(v.name || ' ' || v.summary || ' ' || array_to_string(v.tags, ' ')),
--               lower(:keyword)) > 0)
--   AND (
--     :is_system_admin OR a.visibility_scope = 'all'
--     OR (:is_org_admin AND a.organization_id = :organization_id)
--     OR EXISTS (SELECT 1 FROM scene_visibility_target t WHERE t.application_id = a.id
--       AND ((t.scope = 'members' AND t.target_id = :actor_subject)
--         OR (t.scope = 'organizations' AND t.target_id = :organization_id)
--         OR (t.scope = 'projects' AND t.target_id = ANY(CAST(:project_ids AS varchar[])))))
--   )
-- ORDER BY m.last_downloaded_at DESC NULLS LAST, a.id LIMIT :page_size OFFSET :offset;
-- 下载量/收藏数排序分别改为 m.download_count / m.favorite_count DESC，再加 a.id 稳定排序。
-- 综合排序按当前配置推荐顺序和原业务评分规则在仓储查询中实现，不接受任意前端 SQL 字段。
-- strpos 保持当前“不区分大小写的字面子串”语义；中文包含搜索不会被普通 B-tree 加速。
-- 当前无扩展依赖；真实数据量需要时再选 pg_trgm/检索服务，并用 EXPLAIN 验证。
--
-- B. 收藏写入前检查当前用户的应用访问权限，再原子写入；一人一应用不会重复计数。
-- INSERT INTO scene_interaction(application_id, actor_subject, favorite, favorited_at)
-- VALUES (:application_id, :actor_subject, true, now())
-- ON CONFLICT (application_id, actor_subject)
-- DO UPDATE SET favorite = true,
--   favorited_at = coalesce(scene_interaction.favorited_at, EXCLUDED.favorited_at), updated_at = now();
-- 取消收藏：UPDATE ... SET favorite = false, favorited_at = NULL, updated_at = now() WHERE ...;
--
-- C. 草稿/规则修改必须匹配 revision；更新 0 行即冲突，后端返回 409。
-- UPDATE scene_publication SET revision = revision + 1, updated_at = now(),
--   last_edited_by_subject = :actor_subject
-- WHERE id = :publication_id AND revision = :expected_revision
--   AND status IN ('draft','rejected','needs_changes') RETURNING revision;
-- 版本资料、依赖更新必须与此语句同一事务；无返回行则整体回滚。
--
-- D. 审核通过事务顺序（不是单独执行一个状态 UPDATE）：
-- BEGIN -> 锁 scene_application -> 锁 scene_publication -> 检查 pending/revision/base_version_id
-- -> 校验依赖、资料、权限、三项评分、当前阈值 -> INSERT scene_review
-- -> UPDATE scene_publication SET status='approved', revision=revision+1
-- -> UPDATE scene_application_version SET released_at=now(), updated_at=now()
-- -> UPDATE scene_application SET current_version_id=:version_id, revision=revision+1, updated_at=now()
-- -> INSERT scene_notification（审核结果及订阅新版消息） -> COMMIT。
-- 审核拒绝/补材料也写审核快照与通知，但不改当前上架版本。
--
-- E. 运行幂等：先 INSERT ... ON CONFLICT DO NOTHING RETURNING id 并提交 running 记录。
-- 只有插入成功的工作者调用供应方；冲突时读取原记录，比较 fingerprint，不能重复调用。
-- 外部请求放在数据库事务之外；拿到结果再按原状态条件更新，完成后不可重复结算。
-- 请求 SHA-256 不含“重试时的当前版本 ID”；复用请求 ID 应返回最初那次绑定的版本和结果。
-- unknown/running 超时需查询供应方结果或利用供应方幂等键恢复，不得直接重放扣费请求。
--
-- F. 我的未读消息：角色判断由可信身份提供；不存在个人回执就算未读。
-- SELECT n.* FROM scene_notification n
-- LEFT JOIN scene_notification_receipt r
--   ON r.notification_id = n.id AND r.actor_subject = :actor_subject
-- WHERE r.notification_id IS NULL
--   AND ((n.recipient_kind='user' AND n.recipient_id=:actor_subject)
--     OR (:is_system_admin AND n.recipient_kind='role' AND n.recipient_id='admin'));
-- 返回前继续按应用开放范围过滤；属于本人的审核通知允许在应用尚未上架时查看。
-- 标记已读先验证接收权限，再 INSERT receipt ... ON CONFLICT DO NOTHING。
--
-- 生产接入事项：此脚本通过后仍需 ORM、仓储适配与 Alembic 迁移。
-- 所有审计/版本关联默认 NO ACTION，不随删除应用级联清空历史；当前没有删除业务入口。
-- 批量归档/脱敏另做受控维护迁移。主备共用这套表，由 PostgreSQL 复制；Redis 不做事实来源。
