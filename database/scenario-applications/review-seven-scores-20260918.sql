-- 应用审核改为七项评分，取消及格线；保留旧审核记录的三项分数及当时规则。
-- 可重复执行；先迁移数据库，再重启 API，以便重新反射表结构。
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

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

-- 默认版本 1 兼容尚未更新的服务；新代码显式写版本 2，不改写历史评分或意见。
ALTER TABLE scene_review_note ADD COLUMN IF NOT EXISTS score_schema smallint NOT NULL DEFAULT 1;
ALTER TABLE scene_review_note ALTER COLUMN score_schema SET DEFAULT 1;
ALTER TABLE scene_review_note ADD COLUMN IF NOT EXISTS quality_scores jsonb;
ALTER TABLE scene_review_note ALTER COLUMN minimum_score DROP NOT NULL;
ALTER TABLE scene_review_note DROP CONSTRAINT IF EXISTS ck_scene_review_scores;
ALTER TABLE scene_review_note DROP CONSTRAINT IF EXISTS ck_scene_review_approve;
ALTER TABLE scene_review_note ADD CONSTRAINT ck_scene_review_scores CHECK (
    (score_schema = 1 AND quality_scores IS NULL
        AND num_nonnulls(completeness_score, usability_score, security_score) IN (0,3))
    OR (score_schema = 2 AND quality_scores IS NOT NULL
        AND COALESCE(scene_valid_review_scores(quality_scores), false)
        AND num_nonnulls(completeness_score, usability_score, security_score, minimum_score) = 0)
);
ALTER TABLE scene_review_note ADD CONSTRAINT ck_scene_review_approve CHECK (
    score_schema = 2 OR recommendation <> 'approved' OR (
        completeness_score IS NOT NULL AND usability_score IS NOT NULL AND security_score IS NOT NULL
        AND minimum_score IS NOT NULL
        AND completeness_score >= minimum_score AND usability_score >= minimum_score AND security_score >= minimum_score
    )
);
COMMENT ON COLUMN scene_review_note.score_schema IS '1=历史三项评分；2=七项评分，不设及格线';
COMMENT ON COLUMN scene_review_note.quality_scores IS '七项完整整数评分（1–5），审核结论由审核员决定';
COMMENT ON COLUMN scene_review_note.minimum_score IS '仅保留历史审核门槛，新审核为空';
COMMIT;
