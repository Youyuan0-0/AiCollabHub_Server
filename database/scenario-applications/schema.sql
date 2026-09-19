-- 场景应用数据库设计 V2.1：按已提供接口保存最少本地数据，2026-09-15
-- PostgreSQL 16/17，UTF-8。共 9 张本地业务表，2 个统计视图（含教程附件索引）。
-- 目标：Agent Dev 提供智能体/版本/运行，HiMarket 提供分类/目录/草稿/发布/审核/资源包。
-- 这是接入目标设计，不代表现场已开通 HiMarket；现有演示代码尚未使用这些表。
-- 本地保存关联编号、平台扩展、权限、个人操作、补充审核和必要记录。
-- 不建立外部资源版本库、模型库、知识库、数据集库，也不复制完整外部响应。
-- 本版去掉目录缓存列、人员姓名副本和本地分类字典；本地扩展限定字段白名单。
-- 原文字段归属与页码见 interface-evidence.md；功能代码仍需按此边界适配。
-- 首次建表：psql -X -v ON_ERROR_STOP=1 -d <开发主库名> -f schema.sql
-- 本文件不执行 DROP/TRUNCATE，不修改现有认证表；同名表存在时失败，不能覆盖 V1 库。
-- V1 若已执行，须另写显式迁移，不能用本脚本冒充升级；当前场景业务尚未执行 V1。
-- 所有 ID 由后端生成；*_subject 关联可信 IAM subject，不关联临时 auth_session。
-- 组织/项目/用户主表由公共身份模块统一提供，本版不重复建，暂保存其编号。
-- API 地址与凭证引用放后端配置，下面的 *_profile 只是配置项名称，不是密钥。
-- updated_at 与 revision 由仓储在更新时显式维护；时间用 timestamptz。

BEGIN;
SET LOCAL search_path = public;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- 01 应用关联与扩展：每个本地应用最多绑定一个 Agent Dev 资源、一个 HiMarket Listing。
-- 一对多外部来源不是当前需要；未来确有此需求时再拆独立绑定表。
CREATE TABLE scene_application (
    id varchar(80) PRIMARY KEY,                     -- 本地稳定应用编号
    created_by_subject varchar(200) NOT NULL,       -- 本地登记人的可信用户编号；姓名从公共 IAM 获取
    organization_id varchar(200),                   -- 平台登记的责任组织编号；组织资料从公共 IAM 获取
    agent_profile varchar(80),                      -- Agent Dev 系统/环境/租户对应的后端配置名
    workspace_code varchar(200),                    -- Agent Dev 工作空间编号
    agent_code varchar(200),                        -- Agent Dev agentCode
    market_profile varchar(80),                     -- HiMarket 系统/环境/租户对应的后端配置名
    market_resource_id varchar(200),                -- HiMarket resourceId，不能当成 agentCode
    market_listing_id varchar(200),                 -- HiMarket listingId，可晚于 resourceId 取得
    local_metadata jsonb NOT NULL DEFAULT '{}',     -- 仅已生效的平台补充项；白名单见下面约束
    visibility_scope varchar(20) NOT NULL DEFAULT 'all',
    visibility_revision bigint NOT NULL DEFAULT 1 CHECK (visibility_revision > 0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_application_scope UNIQUE (id, visibility_scope),
    CONSTRAINT uq_scene_application_agent UNIQUE (agent_profile, workspace_code, agent_code),
    CONSTRAINT uq_scene_application_listing UNIQUE (market_profile, market_listing_id),
    CONSTRAINT ck_scene_application_agent CHECK (
        num_nonnulls(agent_profile, workspace_code, agent_code) = 0
        OR (num_nonnulls(agent_profile, workspace_code, agent_code) = 3
            AND btrim(agent_profile) <> '' AND btrim(workspace_code) <> '' AND btrim(agent_code) <> '')
    ),
    CONSTRAINT ck_scene_application_market CHECK (
        num_nonnulls(market_profile, market_resource_id, market_listing_id) = 0
        OR (market_profile IS NOT NULL AND market_resource_id IS NOT NULL
            AND btrim(market_profile) <> '' AND btrim(market_resource_id) <> ''
            AND (market_listing_id IS NULL OR btrim(market_listing_id) <> ''))
    ),
    CONSTRAINT ck_scene_application_metadata CHECK (jsonb_typeof(local_metadata) = 'object'),
    CONSTRAINT ck_scene_application_local_fields CHECK (
        local_metadata - ARRAY['englishName','contact','tutorials','usageLimits','technicalStack',
            'deployment','triggerType','dependencies','configOptions']::text[] = '{}'::jsonb
    ),
    CONSTRAINT ck_scene_application_scope CHECK (visibility_scope IN ('all','organizations','members','projects')),
    CONSTRAINT ck_scene_application_owner CHECK (
        btrim(created_by_subject) <> '' AND (organization_id IS NULL OR btrim(organization_id) <> '')
    )
);
CREATE INDEX ix_scene_application_owner ON scene_application(created_by_subject, updated_at DESC);
CREATE INDEX ix_scene_application_org ON scene_application(organization_id, updated_at DESC);
COMMENT ON TABLE scene_application IS '外部应用对应关系和本地扩展；不维护另一套外部目录及版本状态';
COMMENT ON COLUMN scene_application.local_metadata IS
    '仅白名单补充字段；name/summary/descriptionMarkdown/category/tags/version/inputSchema/outputSchema 等由外部提供。教程只存链接，依赖只存编号，配置项只作说明。嵌套结构须经后端校验。待审修改存 submission_extension';

-- 02 开放对象：复用 IAM 编号，补充 PRIVATE/PUBLIC 无法直接表达的本地范围。
CREATE TABLE scene_visibility_target (
    application_id varchar(80) NOT NULL,
    scope varchar(20) NOT NULL CHECK (scope IN ('organizations','members','projects')),
    target_id varchar(200) NOT NULL CHECK (btrim(target_id) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, scope, target_id),
    CONSTRAINT fk_scene_visibility_application FOREIGN KEY (application_id, scope)
        REFERENCES scene_application(id, visibility_scope) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_scene_visibility_lookup ON scene_visibility_target(scope, target_id, application_id);
COMMENT ON TABLE scene_visibility_target IS '本地应用可见的组织、成员或项目；外部使用授权仍须另行校验';

-- 03 个人互动：关注更新与 HiMarket 申请使用权限的 Subscription 分开。
CREATE TABLE scene_interaction (
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    actor_subject varchar(200) NOT NULL,
    favorite boolean NOT NULL DEFAULT false,
    follow_updates boolean NOT NULL DEFAULT false,   -- 当前前端 subscribed 的含义：关注新版消息
    rating smallint CHECK (rating BETWEEN 1 AND 5),
    comment varchar(1000) NOT NULL DEFAULT '',
    favorited_at timestamptz,
    followed_at timestamptz,
    rated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, actor_subject),
    CONSTRAINT ck_scene_interaction_favorite CHECK (favorite = (favorited_at IS NOT NULL)),
    CONSTRAINT ck_scene_interaction_follow CHECK (follow_updates = (followed_at IS NOT NULL)),
    CONSTRAINT ck_scene_interaction_rating CHECK (
        (rating IS NULL AND rated_at IS NULL AND comment = '')
        OR (rating IS NOT NULL AND rated_at IS NOT NULL)
    )
);
CREATE INDEX ix_scene_interaction_favorite ON scene_interaction(actor_subject, favorited_at DESC) WHERE favorite;
CREATE INDEX ix_scene_interaction_follow ON scene_interaction(actor_subject, followed_at DESC) WHERE follow_updates;
CREATE INDEX ix_scene_interaction_followers ON scene_interaction(application_id, actor_subject) WHERE follow_updates;
COMMENT ON TABLE scene_interaction IS '本平台收藏、关注更新、五分评分与评价；不重复管理 HiMarket 授权订阅';

-- 04 发布补充资料：只保存平台特有内容及提交时规则，外部草稿/版本/状态由 HiMarket 管理。
-- 首次编辑可暂缺外部审核单 ID；发起外部提交前冻结本地补充资料，返回 ID 后补登记。
-- 补材料重提新增 round_no 行，保留上次内容；不是再建完整的资源版本表。
CREATE TABLE scene_submission_extension (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    submission_key varchar(80) NOT NULL,             -- 同一次本地发布事项的稳定编号
    round_no integer NOT NULL DEFAULT 1 CHECK (round_no > 0),
    created_by_subject varchar(200) NOT NULL,
    market_profile varchar(80),                      -- 提交当时的环境快照
    external_submission_id varchar(200),             -- HiMarket submissionId；不是本地主键
    external_version_id varchar(200),                -- HiMarket 本次审核的 versionId
    agent_version_code varchar(200),                 -- 如有关联，保存 Agent Dev agentVersion
    local_fields jsonb NOT NULL DEFAULT '{}',        -- 同 local_metadata 白名单；不保存完整外部草稿
    form_rules_snapshot jsonb NOT NULL DEFAULT '{}', -- 必填/提示规则及版本
    security_level varchar(20) NOT NULL DEFAULT 'internal',
    priority varchar(10) NOT NULL DEFAULT 'normal',
    frozen_at timestamptz,                          -- 准备提交的时刻；此后补充内容不可覆盖
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_extension_round UNIQUE (application_id, submission_key, round_no),
    CONSTRAINT uq_scene_extension_external_round UNIQUE (market_profile, external_submission_id, round_no),
    CONSTRAINT uq_scene_extension_application UNIQUE (application_id, id),
    CONSTRAINT ck_scene_extension_json CHECK (
        jsonb_typeof(local_fields) = 'object' AND jsonb_typeof(form_rules_snapshot) = 'object'
    ),
    CONSTRAINT ck_scene_extension_local_fields CHECK (
        local_fields - ARRAY['englishName','contact','tutorials','usageLimits','technicalStack',
            'deployment','triggerType','dependencies','configOptions']::text[] = '{}'::jsonb
    ),
    CONSTRAINT ck_scene_extension_security CHECK (security_level IN ('internal','public','confidential')),
    CONSTRAINT ck_scene_extension_priority CHECK (priority IN ('normal','high','urgent')),
    CONSTRAINT ck_scene_extension_external CHECK (
        external_submission_id IS NULL OR (market_profile IS NOT NULL AND btrim(market_profile) <> ''
            AND btrim(external_submission_id) <> '' AND frozen_at IS NOT NULL)
    ),
    CONSTRAINT ck_scene_extension_frozen_security CHECK (frozen_at IS NULL OR security_level <> 'confidential')
);
CREATE INDEX ix_scene_extension_external ON scene_submission_extension(market_profile, external_submission_id);
CREATE UNIQUE INDEX uq_scene_extension_draft ON scene_submission_extension(application_id) WHERE frozen_at IS NULL;
CREATE INDEX ix_scene_extension_owner ON scene_submission_extension(created_by_subject, updated_at DESC);
COMMENT ON TABLE scene_submission_extension IS '本地发布补充项及重提快照；没有第二套权威审核或发布状态';

-- 05 业务操作记录：运行、下载、同步和写入外部服务使用共同的请求/恢复字段。
-- 类型相关内容只按需要保存到 detail/request_data/result_data，不整包持久化供应方内部配置。
CREATE TABLE scene_activity (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) REFERENCES scene_application(id), -- 同步未识别本地应用时允许为空
    submission_extension_id varchar(80),             -- 发布/审核相关操作引用本地补充记录
    operation varchar(20) NOT NULL,
    actor_subject varchar(200) NOT NULL,
    organization_id varchar(200),
    request_id varchar(100) NOT NULL CHECK (char_length(request_id) BETWEEN 8 AND 100),
    idempotency_scope varchar(80) GENERATED ALWAYS AS
        (CASE WHEN operation = 'sync' THEN '@sync' ELSE application_id END) STORED,
    request_fingerprint char(64) NOT NULL CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    mode varchar(20) NOT NULL CHECK (mode IN ('simulation','live')),
    status varchar(20) NOT NULL DEFAULT 'queued',
    provider_profile varchar(80),                    -- 调用的后端环境配置名
    workspace_code varchar(200),                     -- 同步的外部工作空间
    agent_code varchar(200),                         -- 同步/调用的外部智能体编号
    agent_version_code varchar(200),                 -- 发起时锁定的外部运行版本
    market_version_id varchar(200),                  -- 发起时相关 HiMarket 版本编号
    external_request_id varchar(300),
    external_session_id varchar(300),
    external_submission_id varchar(200),
    request_data jsonb,                             -- 外部重试/历史需要时保存已脱敏输入；无需则 NULL
    result_data jsonb,                              -- 个人历史或相同请求重放需要的结果，可设置保留期
    detail jsonb NOT NULL DEFAULT '{}',             -- 同步字段映射/下载文件名等类型特有的少量字段
    error_code varchar(100),
    error_message text NOT NULL DEFAULT '',
    duration_ms bigint CHECK (duration_ms >= 0),
    retry_of_id varchar(80) REFERENCES scene_activity(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    deadline_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_activity_request UNIQUE (actor_subject, operation, idempotency_scope, request_id),
    CONSTRAINT fk_scene_activity_extension FOREIGN KEY (application_id, submission_extension_id)
        REFERENCES scene_submission_extension(application_id, id),
    CONSTRAINT ck_scene_activity_extension CHECK (submission_extension_id IS NULL OR application_id IS NOT NULL),
    CONSTRAINT ck_scene_activity_operation CHECK (
        operation IN ('run','download','sync','save_draft','submit_review','review_decision','publish','subscribe')
    ),
    CONSTRAINT ck_scene_activity_application CHECK (operation = 'sync' OR application_id IS NOT NULL),
    CONSTRAINT ck_scene_activity_sync_target CHECK (operation <> 'sync' OR (
        provider_profile IS NOT NULL AND btrim(provider_profile) <> ''
        AND workspace_code IS NOT NULL AND btrim(workspace_code) <> ''
        AND agent_code IS NOT NULL AND btrim(agent_code) <> ''
        AND agent_version_code IS NOT NULL AND btrim(agent_version_code) <> ''
    )),
    CONSTRAINT ck_scene_activity_status CHECK (
        status IN ('queued','running','succeeded','failed','unchanged','timed_out','unknown')
    ),
    CONSTRAINT ck_scene_activity_unchanged CHECK (status <> 'unchanged' OR operation = 'sync'),
    CONSTRAINT ck_scene_activity_completion CHECK (
        (status IN ('succeeded','failed','unchanged','timed_out') AND completed_at IS NOT NULL)
        OR (status IN ('queued','running','unknown') AND completed_at IS NULL)
    ),
    CONSTRAINT ck_scene_activity_json CHECK (
        (request_data IS NULL OR jsonb_typeof(request_data) = 'object') AND jsonb_typeof(detail) = 'object'
    ),
    CONSTRAINT ck_scene_activity_retry CHECK (retry_of_id IS NULL OR retry_of_id <> id),
    CONSTRAINT ck_scene_activity_time CHECK (completed_at IS NULL OR completed_at >= created_at)
);
CREATE INDEX ix_scene_activity_personal ON scene_activity(actor_subject, operation, created_at DESC);
CREATE INDEX ix_scene_activity_application ON scene_activity(application_id, operation, mode, status, created_at DESC);
CREATE INDEX ix_scene_activity_org ON scene_activity(organization_id, created_at DESC);
CREATE INDEX ix_scene_activity_recovery ON scene_activity(deadline_at) WHERE status IN ('queued','running','unknown');
CREATE INDEX ix_scene_activity_extension ON scene_activity(application_id, submission_extension_id);
CREATE INDEX ix_scene_activity_retry ON scene_activity(retry_of_id);
COMMENT ON TABLE scene_activity IS '本平台业务请求与任务记录；复用恢复机制但按操作类型执行各自规则';
COMMENT ON COLUMN scene_activity.status IS '调用结果状态；外部提交成功不代表资源审核通过，后者应查询 HiMarket';
COMMENT ON COLUMN scene_activity.idempotency_scope IS
    '同步按用户和请求去重，其他操作按用户/类型/应用/请求去重；迟到的 application_id 不改变同步去重范围';

-- 06 审核补充留痕：记录本平台三项评分及建议，不把本地建议当作 HiMarket 最终审批状态。
CREATE OR REPLACE FUNCTION scene_valid_review_scores(value jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN jsonb_typeof(value) = 'object' THEN
        value ?& ARRAY['formatCompliance','safetyCompliance','annotationCompliance',
            'structuralCompleteness','authenticity','consistency','cleanliness']
        AND value - ARRAY['formatCompliance','safetyCompliance','annotationCompliance',
            'structuralCompleteness','authenticity','consistency','cleanliness'] = '{}'::jsonb
        AND value->'formatCompliance' IN ('1','2','3','4','5')
        AND value->'safetyCompliance' IN ('1','2','3','4','5')
        AND value->'annotationCompliance' IN ('1','2','3','4','5')
        AND value->'structuralCompleteness' IN ('1','2','3','4','5')
        AND value->'authenticity' IN ('1','2','3','4','5')
        AND value->'consistency' IN ('1','2','3','4','5')
        AND value->'cleanliness' IN ('1','2','3','4','5')
    ELSE false END
$$;

CREATE TABLE scene_review_note (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL,
    submission_extension_id varchar(80) NOT NULL,
    decision_activity_id varchar(80),                -- 如发起外部审批，关联持久化操作，结果查询 activity
    reviewer_subject varchar(200) NOT NULL,
    recommendation varchar(20) NOT NULL CHECK (recommendation IN ('approved','rejected','needs_changes')),
    reason varchar(2000) NOT NULL DEFAULT '',
    completeness_score smallint CHECK (completeness_score BETWEEN 1 AND 5),
    usability_score smallint CHECK (usability_score BETWEEN 1 AND 5),
    security_score smallint CHECK (security_score BETWEEN 1 AND 5),
    minimum_score smallint CHECK (minimum_score BETWEEN 1 AND 5),
    score_schema smallint NOT NULL DEFAULT 1,
    quality_scores jsonb,
    policy_snapshot jsonb NOT NULL CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    evidence_snapshot jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(evidence_snapshot) = 'object'),
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_scene_review_extension FOREIGN KEY (application_id, submission_extension_id)
        REFERENCES scene_submission_extension(application_id, id),
    CONSTRAINT fk_scene_review_activity FOREIGN KEY (decision_activity_id) REFERENCES scene_activity(id),
    CONSTRAINT uq_scene_review_round UNIQUE (submission_extension_id),
    CONSTRAINT ck_scene_review_scores CHECK (
    (score_schema = 1 AND quality_scores IS NULL
        AND num_nonnulls(completeness_score, usability_score, security_score) IN (0,3))
    OR (score_schema = 2 AND quality_scores IS NOT NULL
        AND COALESCE(scene_valid_review_scores(quality_scores), false)
        AND num_nonnulls(completeness_score, usability_score, security_score, minimum_score) = 0)
),
    CONSTRAINT ck_scene_review_approve CHECK (
    score_schema = 2 OR recommendation <> 'approved' OR (
        completeness_score IS NOT NULL AND usability_score IS NOT NULL AND security_score IS NOT NULL
        AND minimum_score IS NOT NULL
        AND completeness_score >= minimum_score AND usability_score >= minimum_score AND security_score >= minimum_score
    )
),
    CONSTRAINT ck_scene_review_reason CHECK (recommendation = 'approved' OR btrim(reason) <> '')
);
CREATE INDEX ix_scene_review_history ON scene_review_note(reviewer_subject, reviewed_at DESC);
CREATE INDEX ix_scene_review_application ON scene_review_note(application_id, reviewed_at DESC);
CREATE INDEX ix_scene_review_activity ON scene_review_note(decision_activity_id);
COMMENT ON TABLE scene_review_note IS '本地质量评分、补材料意见及审计证据；不重建完整 HiMarket 审批状态机';
COMMENT ON COLUMN scene_review_note.evidence_snapshot IS
    '按审计需要保留当时相关字段及外部版本引用；不是每次查询都复制整份外部资源';

-- 07 站内消息：每个接收人一行，read_at 在本行保存，不再建已读回执表。
CREATE TABLE scene_notification (
    id varchar(80) PRIMARY KEY,
    application_id varchar(80) NOT NULL REFERENCES scene_application(id),
    actor_subject varchar(200) NOT NULL,             -- 具体接收人，不使用 @system-admin 广播角色作用户
    event_key varchar(300) NOT NULL,
    kind varchar(20) NOT NULL CHECK (kind IN ('review','new_version','run_failure')),
    title text NOT NULL,
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_scene_notification_recipient UNIQUE (actor_subject, event_key)
);
CREATE INDEX ix_scene_notification_unread ON scene_notification(actor_subject, created_at DESC) WHERE read_at IS NULL;
CREATE INDEX ix_scene_notification_application ON scene_notification(application_id);
COMMENT ON TABLE scene_notification IS '本平台个人站内通知；管理员消息发送时展开接收人，分别标记已读';

-- 08 平台配置：仅表单、管理列、展示/本地评分/通知规则；分类走 HiMarket 分类接口。
CREATE TABLE scene_configuration (
    key varchar(80) PRIMARY KEY,                     -- form/workflow
    value jsonb NOT NULL CHECK (jsonb_typeof(value) = 'object'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_by_subject varchar(200),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_scene_configuration_key CHECK (key IN ('form','workflow')),
    CONSTRAINT ck_scene_configuration_local_fields CHECK (
        (key = 'form' AND value - ARRAY['requiredFields','fieldHints','managementColumns']::text[] = '{}'::jsonb)
        OR (key = 'workflow' AND value - ARRAY['toolchainUrl','announcement','recommendedIds',
            'hotCategoryRefs','defaultSort','pageSize','sensitiveTerms','minimumQualityScore',
            'notificationsEnabled','notificationTemplate','failureThreshold']::text[] = '{}'::jsonb)
    )
);
COMMENT ON TABLE scene_configuration IS '平台表单、列表、运营及补充审核配置；不存外部分类别名或分类字典。热门分类仅引用 marketProfile/categoryId；配置结构继续由后端校验';

-- 请求身份和已建立的关联不可被后续结果写入改掉；仅允许未识别的同步首次补本地应用。
CREATE FUNCTION scene_guard_activity_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id,NEW.actor_subject,NEW.operation,NEW.request_id,NEW.request_fingerprint,NEW.mode)
       IS DISTINCT FROM ROW(OLD.id,OLD.actor_subject,OLD.operation,OLD.request_id,OLD.request_fingerprint,OLD.mode)
       OR (OLD.application_id IS NOT NULL AND NEW.application_id IS DISTINCT FROM OLD.application_id)
       OR (OLD.submission_extension_id IS NOT NULL AND NEW.submission_extension_id IS DISTINCT FROM OLD.submission_extension_id)
    THEN RAISE EXCEPTION '已登记请求的身份、指纹和归属不可改写' USING ERRCODE='23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scene_activity_identity BEFORE UPDATE ON scene_activity
    FOR EACH ROW EXECUTE FUNCTION scene_guard_activity_identity();

-- 提交补充资料冻结；允许稍后登记/补齐外部审核编号，不允许替换已有外部归属。
CREATE FUNCTION scene_freeze_submission_extension() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.frozen_at IS NOT NULL THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION '已提交补充资料不可删除' USING ERRCODE = '23514';
        END IF;
        IF ROW(NEW.id,NEW.application_id,NEW.submission_key,NEW.round_no,NEW.created_by_subject,
               NEW.local_fields,NEW.form_rules_snapshot,NEW.security_level,NEW.priority,NEW.frozen_at)
           IS DISTINCT FROM ROW(OLD.id,OLD.application_id,OLD.submission_key,OLD.round_no,OLD.created_by_subject,
               OLD.local_fields,OLD.form_rules_snapshot,OLD.security_level,OLD.priority,OLD.frozen_at)
           OR (OLD.market_profile IS NOT NULL AND NEW.market_profile IS DISTINCT FROM OLD.market_profile)
           OR (OLD.external_submission_id IS NOT NULL AND NEW.external_submission_id IS DISTINCT FROM OLD.external_submission_id)
           OR (OLD.external_version_id IS NOT NULL AND NEW.external_version_id IS DISTINCT FROM OLD.external_version_id)
           OR (OLD.agent_version_code IS NOT NULL AND NEW.agent_version_code IS DISTINCT FROM OLD.agent_version_code)
        THEN RAISE EXCEPTION '已提交资料不可覆盖，补材料请新增轮次' USING ERRCODE = '23514'; END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scene_extension_freeze BEFORE UPDATE OR DELETE ON scene_submission_extension
    FOR EACH ROW EXECUTE FUNCTION scene_freeze_submission_extension();

CREATE FUNCTION scene_guard_review_note() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE frozen_time timestamptz;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION '审核补充记录只能新增' USING ERRCODE = '23514';
    END IF;
    SELECT frozen_at INTO frozen_time FROM scene_submission_extension
        WHERE id = NEW.submission_extension_id FOR UPDATE;
    IF frozen_time IS NULL THEN
        RAISE EXCEPTION '审核前必须冻结对应轮次的补充资料' USING ERRCODE = '23514';
    END IF;
    IF NEW.decision_activity_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM scene_activity WHERE id=NEW.decision_activity_id
        AND application_id=NEW.application_id AND submission_extension_id=NEW.submission_extension_id
        AND operation='review_decision'
    ) THEN RAISE EXCEPTION '审批操作必须关联本申请的 review_decision 记录' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER scene_review_guard BEFORE INSERT OR UPDATE OR DELETE ON scene_review_note
    FOR EACH ROW EXECUTE FUNCTION scene_guard_review_note();

-- 开放范围在事务提交时核对，允许一个事务中切换类型并替换目标。
CREATE FUNCTION scene_check_visibility() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE app_id varchar(80); scope_value varchar(20);
BEGIN
    IF TG_TABLE_NAME='scene_application' THEN app_id:=NEW.id;
    ELSIF TG_OP='DELETE' THEN app_id:=OLD.application_id;
    ELSE app_id:=NEW.application_id; END IF;
    SELECT visibility_scope INTO scope_value FROM scene_application WHERE id=app_id FOR UPDATE;
    IF FOUND AND scope_value <> 'all' AND NOT EXISTS (
        SELECT 1 FROM scene_visibility_target WHERE application_id=app_id AND scope=scope_value
    ) THEN RAISE EXCEPTION '指定开放范围至少需要一个目标' USING ERRCODE='23514'; END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER scene_application_visibility AFTER INSERT OR UPDATE ON scene_application
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION scene_check_visibility();
CREATE CONSTRAINT TRIGGER scene_target_visibility AFTER INSERT OR DELETE ON scene_visibility_target
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION scene_check_visibility();
CREATE FUNCTION scene_reject_visibility_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '范围目标请在同一事务中删除旧项再新增' USING ERRCODE='23514'; END;
$$;
CREATE TRIGGER scene_visibility_no_update BEFORE UPDATE ON scene_visibility_target
    FOR EACH ROW EXECUTE FUNCTION scene_reject_visibility_update();

-- 只有平台配置，不插入分类字典、演示用户、应用或统计。
-- HiMarket P418/P422 提供分类查询/保存。hotCategoryRefs 只保存
-- {"marketProfile":"配置名","categoryId":"对方编号"} 引用；名称和分类详情通过接口取。
INSERT INTO scene_configuration(key,value) VALUES
('form','{"requiredFields":["name","version","category","owner","summary"],"fieldHints":{},"managementColumns":["category","owner","version","usage","updatedAt"]}'),
('workflow','{"toolchainUrl":"","announcement":"","recommendedIds":[],"hotCategoryRefs":[],"defaultSort":"recommended","pageSize":12,"sensitiveTerms":[],"notificationsEnabled":true,"notificationTemplate":"{name}：{status}","failureThreshold":3}');

-- 视图只汇总本平台实际发生的操作，不充当对方全局计量。
CREATE VIEW scene_run_metrics AS
SELECT application_id, mode,
    count(*) FILTER (WHERE status='succeeded') AS success_count,
    count(*) FILTER (WHERE status IN ('failed','timed_out')) AS failure_count,
    count(*) FILTER (WHERE status IN ('queued','running','unknown')) AS unresolved_count,
    round(avg(duration_ms) FILTER (WHERE status IN ('succeeded','failed','timed_out')),2) AS average_duration_ms,
    round(100.0 * count(*) FILTER (WHERE status='succeeded')
        / NULLIF(count(*) FILTER (WHERE status IN ('succeeded','failed','timed_out')),0),2) AS success_rate_percent
FROM scene_activity WHERE operation='run' GROUP BY application_id,mode;
COMMENT ON VIEW scene_run_metrics IS '仅运行操作；模拟/真实分开，unknown 不计作确定失败，无完成样本时指标为空';

CREATE VIEW scene_application_metrics AS
SELECT a.id AS application_id,
    coalesce(i.favorite_count,0) AS favorite_count, coalesce(i.follower_count,0) AS follower_count,
    coalesce(i.rating_count,0) AS rating_count, i.rating_average,
    coalesce(d.download_count,0) AS download_count, d.last_downloaded_at,
    coalesce(r.success_count,0) AS live_usage_count, coalesce(s.success_count,0) AS simulation_usage_count
FROM scene_application a
LEFT JOIN (
    SELECT application_id, count(*) FILTER (WHERE favorite) AS favorite_count,
        count(*) FILTER (WHERE follow_updates) AS follower_count,
        count(rating) AS rating_count,round(avg(rating),2) AS rating_average
    FROM scene_interaction GROUP BY application_id
) i ON i.application_id=a.id
LEFT JOIN (
    SELECT application_id,count(*) AS download_count,max(completed_at) AS last_downloaded_at
    FROM scene_activity WHERE operation='download' AND status='succeeded' AND mode='live' GROUP BY application_id
) d ON d.application_id=a.id
LEFT JOIN scene_run_metrics r ON r.application_id=a.id AND r.mode='live'
LEFT JOIN scene_run_metrics s ON s.application_id=a.id AND s.mode='simulation';
COMMENT ON VIEW scene_application_metrics IS '平台互动/真实资料下载/分别统计的使用量；先聚合后关联，避免数量乘算';

COMMIT;

-- ================== 常用查询及实现约定（以下均为说明，不自动执行） ==================
-- 1. 基础名称/版本/发布状态优先查询 HiMarket/Agent Dev；授权状态每次按可信身份校验。
--    不能拿 scene_application 存在或本地评分通过，就判断已发布、已授权。
--    分类增删改查、Markdown 说明和资源包下载使用 HiMarket，不维护第二份分类/正文/资源包。
--    缓存按后续性能需求放缓存服务，并设有效期；本版业务表不保存外部目录投影。
-- 2. 本地范围校验示例（:xxx 为 SQLAlchemy 参数，不是字符串拼接；管理员规则另用可信身份加入）：
-- SELECT a.id FROM scene_application a WHERE a.id=:application_id AND (
--   a.visibility_scope='all' OR EXISTS (
--     SELECT 1 FROM scene_visibility_target t WHERE t.application_id=a.id AND (
--       (t.scope='members' AND t.target_id=:actor_subject)
--       OR (t.scope='organizations' AND t.target_id=:organization_id)
--       OR (t.scope='projects' AND t.target_id=ANY(CAST(:project_ids AS varchar[])))
--     )
--   )
-- );
-- 有本地限制/收藏排序时必须在最终分页前处理全体候选，不能过滤上游一页后冒充完整分页。
-- 优先使用外部筛选/排序；本地收藏排序仍需组合完整候选，规模化投影须另行设计，不隐式整包入库。
--
-- 3. 收藏（先检查访问权限）：
-- INSERT INTO scene_interaction(application_id,actor_subject,favorite,favorited_at)
-- VALUES (:application_id,:actor_subject,true,now())
-- ON CONFLICT (application_id,actor_subject) DO UPDATE SET favorite=true,
--   favorited_at=coalesce(scene_interaction.favorited_at,EXCLUDED.favorited_at),updated_at=now();
-- 取消收藏时同时设 favorite=false、favorited_at=NULL。关注与评分分别更新，不互相覆盖。
--
-- 4. revision 乐观锁（返回 0 行则业务返回 409，不继续写入同一事务的其他内容）：
-- UPDATE scene_configuration SET value=CAST(:value AS jsonb),revision=revision+1,
--   updated_by_subject=:actor_subject,updated_at=now()
-- WHERE key=:key AND revision=:expected_revision RETURNING revision;
-- JSON 结构、必填字段、分类、推荐引用、敏感词和配置权限仍沿用 Python 业务校验。
--
-- 5. 写外部服务：本地事务登记 activity(queued)+必要补充/审核记录 -> COMMIT
--    -> 工作执行者用条件 UPDATE queued->running 领取 -> 调用外部服务 -> 更新最终结果。
--    不保持数据库事务等待网络；只有成功登记/领取的执行者可以发起调用。
--    相同请求先比较规范化 SHA-256，不同内容返回 409；mode 纳入指纹，不可跨演示/真实重用。
--    同步填入 application_id 后去重范围仍是 @sync，防止身份关联改变导致重复导入。
--    超时/断线结果不明用 unknown，先查询外部状态，不盲目重新执行发布/审批/扣费请求。
--    正式接入仍需供应方幂等支持、worker 领取/恢复、超时和响应映射代码。
--
-- 6. 审核：先冻结本轮补充资料，校验分数，记本地 recommendation 和外部操作请求。
--    recommendation='approved' 只是本地建议通过；以 HiMarket 确认的审核/发布状态决定上架。
--    HiMarket 的 needs_changes 映射未明确时，本地补充闸门应阻止继续发布，不能伪造上游状态。
--    补材料创建新 round_no；旧评分与快照保留。普通已发布资料变更仍应走新的外部版本/审核。
--
-- 7. 未读消息：SELECT * FROM scene_notification
--    WHERE actor_subject=:actor_subject AND read_at IS NULL ORDER BY created_at DESC;
-- 标记：UPDATE scene_notification SET read_at=coalesce(read_at,now())
--    WHERE id=:notification_id AND actor_subject=:actor_subject;
-- 读取/标记还应校验应用可见权限，本人的审核结果在尚未发布时可以查看。
--
-- 8. 外部输入输出只保存历史/幂等/审计所需部分；按保留期清理载荷仍应保留请求幂等标记。
--    清理后重复旧请求不得重新调用，只能返回已处理状态/结果已过期。
--    不存 API 密钥、模型权重、知识正文、向量索引、完整工具链内部定义。
-- 9. 本脚本是首次建表设计，不包含 ORM/仓储/HiMarket 适配实现；主备复制同一套表。
--    如果现场不启用 HiMarket，应重新确定本地目录/版本/发布存储，不能直接套用本接口优先方案。
-- 10. 范式：主体按应用、个人互动、补充提交、审核和操作拆开；用户资料复用公共身份模块。
--     范围类型用于复合外键，规则/证据快照用于历史追溯，JSON 用于有限扩展；不宣称严格全表三范式。
--     此文件只用于尚未创建场景表的库；若已执行 V2，须先做显式数据迁移，不可直接重跑。

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
