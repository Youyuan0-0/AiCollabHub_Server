-- 本文件随 20260920_01 基线冻结；未来变更通过新迁移完成。
CREATE FUNCTION aih_is_string_array(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN jsonb_typeof(value) <> 'array' THEN false ELSE
        NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(value) item
            WHERE jsonb_typeof(item) <> 'string' OR btrim(item #>> '{}') = ''
        ) AND (
            SELECT count(*) = count(DISTINCT item #>> '{}')
            FROM jsonb_array_elements(value) item
        ) END;
$$;

CREATE FUNCTION aih_resource_tables(kind text, OUT main_table text, OUT prefix text)
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT m.main_table, m.prefix FROM (VALUES
        ('dataset', 'datasets', 'dataset'),
        ('model', 'models', 'model'),
        ('application', 'applications', 'application'),
        ('knowledge', 'knowledge_bases', 'knowledge_base'),
        ('tool', 'ai_tools', 'ai_tool')
    ) AS m(kind, main_table, prefix) WHERE m.kind = $1;
$$;

CREATE FUNCTION aih_check_resource_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record; found boolean;
BEGIN
    SELECT * INTO target FROM aih_resource_tables(NEW.resource_type);
    IF target.main_table IS NULL THEN
        RAISE EXCEPTION 'Unknown resource type' USING ERRCODE = '23514';
    END IF;
    -- 与资源删除互斥，避免仅做无锁存在性查询产生悬空引用。
    EXECUTE format('SELECT true FROM %I.%I WHERE id=$1 FOR KEY SHARE',
                   TG_TABLE_SCHEMA, target.main_table) INTO found USING NEW.resource_id;
    IF found IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Referenced resource does not exist' USING ERRCODE = '23503';
    END IF;
    IF TG_TABLE_NAME = 'resource_downloads' THEN
        IF NEW.version_id IS NOT NULL THEN
            EXECUTE format('SELECT true FROM %I.%I WHERE id=$1 AND %I=$2 FOR KEY SHARE',
                           TG_TABLE_SCHEMA, target.prefix || '_versions', target.prefix || '_id')
            INTO found USING NEW.version_id, NEW.resource_id;
            IF found IS DISTINCT FROM true THEN
                RAISE EXCEPTION 'Download version does not belong to resource' USING ERRCODE = '23503';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_restrict_resource_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE child text; found boolean;
BEGIN
    FOREACH child IN ARRAY ARRAY['resource_favorites','resource_follows','resource_ratings','resource_downloads'] LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE resource_type=$1 AND resource_id=$2)',
                       TG_TABLE_SCHEMA, child) INTO found USING TG_ARGV[0], OLD.id;
        IF found THEN
            RAISE EXCEPTION 'Resource still has interaction records' USING ERRCODE = '23503';
        END IF;
    END LOOP;
    RETURN OLD;
END;
$$;

CREATE FUNCTION aih_check_review_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record; publication record;
BEGIN
    SELECT * INTO target FROM aih_resource_tables(NEW.resource_type);
    IF target.prefix IS NULL THEN
        RAISE EXCEPTION 'Unknown resource type' USING ERRCODE = '23514';
    END IF;
    EXECUTE format('SELECT * FROM %I.%I WHERE id=$1 FOR UPDATE',
                   TG_TABLE_SCHEMA, target.prefix || '_publications') INTO publication USING NEW.publication_id;
    IF publication.id IS NULL THEN
        RAISE EXCEPTION 'Referenced publication does not exist' USING ERRCODE = '23503';
    END IF;
    IF publication.review_status IS DISTINCT FROM 'pending'
       OR NEW.submission_no <> publication.submission_no
       OR NEW.submitted_at <> publication.submitted_at THEN
        RAISE EXCEPTION 'Review does not match pending submission' USING ERRCODE = '23514';
    END IF;
    IF NEW.submission_snapshot->'version' IS DISTINCT FROM to_jsonb(publication.version)
       OR NEW.submission_snapshot->'versionNotes' IS DISTINCT FROM coalesce(to_jsonb(publication.version_notes), 'null'::jsonb)
       OR NEW.submission_snapshot->'data' IS DISTINCT FROM publication.data THEN
        RAISE EXCEPTION 'Review snapshot differs from submitted content' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_review_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE publication record; review record; target record; publication_id uuid; kind text;
BEGIN
    IF TG_TABLE_NAME = 'resource_reviews' THEN
        kind := NEW.resource_type;
        publication_id := NEW.publication_id;
    ELSE
        kind := TG_ARGV[0];
        publication_id := NEW.id;
    END IF;
    SELECT * INTO target FROM aih_resource_tables(kind);
    -- 延迟触发器按事务最终状态检查，不使用早期 UPDATE 排队时的旧快照。
    EXECUTE format('SELECT * FROM %I.%I WHERE id=$1', TG_TABLE_SCHEMA, target.prefix || '_publications')
    INTO publication USING publication_id;
    IF publication.id IS NULL THEN
        IF TG_TABLE_NAME = 'resource_reviews' THEN
            RAISE EXCEPTION 'Review lost its publication' USING ERRCODE = '23503';
        END IF;
        RETURN NULL;
    END IF;
    EXECUTE format('SELECT * FROM %I.resource_reviews WHERE resource_type=$1 AND publication_id=$2 AND submission_no=$3',
                   TG_TABLE_SCHEMA) INTO review USING kind, publication_id, publication.submission_no;
    IF publication.review_status IN ('approved','rejected') THEN
        IF review.id IS NULL OR review.decision <> publication.review_status THEN
            RAISE EXCEPTION 'Publication must have a matching review decision' USING ERRCODE = '23514';
        END IF;
    ELSIF review.id IS NOT NULL THEN
        RAISE EXCEPTION 'Review and publication state must commit together' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_check_version_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE publication record;
BEGIN
    EXECUTE format('SELECT * FROM %I.%I WHERE id=$1', TG_TABLE_SCHEMA, TG_ARGV[0])
    INTO publication USING NEW.publication_id;
    IF publication.id IS NULL OR publication.review_status IS DISTINCT FROM 'approved'
       OR publication.published_version_id IS DISTINCT FROM NEW.id
       OR publication.published_at IS DISTINCT FROM NEW.published_at
       OR publication.version IS DISTINCT FROM NEW.version
       OR publication.version_notes IS DISTINCT FROM NEW.version_notes THEN
        RAISE EXCEPTION 'Version must match an approved and published submission' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_freeze_publication() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id <> OLD.id OR NEW.applicant_id <> OLD.applicant_id OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'Publication identity cannot be changed' USING ERRCODE = '23514';
    END IF;
    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'Publication revision must increase by one' USING ERRCODE = '23514';
    END IF;
    IF to_jsonb(OLD)->>TG_ARGV[0] IS NOT NULL
       AND to_jsonb(NEW)->>TG_ARGV[0] IS DISTINCT FROM to_jsonb(OLD)->>TG_ARGV[0] THEN
        RAISE EXCEPTION 'Publication resource cannot be changed' USING ERRCODE = '23514';
    END IF;
    IF OLD.review_status IN ('pending','approved') THEN
        IF ROW(NEW.data,NEW.version,NEW.version_notes,NEW.base_version_id,NEW.submission_no,NEW.submitted_at)
           IS DISTINCT FROM ROW(OLD.data,OLD.version,OLD.version_notes,OLD.base_version_id,OLD.submission_no,OLD.submitted_at) THEN
            RAISE EXCEPTION 'Submitted content is frozen' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF OLD.review_status = 'approved' AND NEW.review_status IS DISTINCT FROM 'approved' THEN
        RAISE EXCEPTION 'Approved publication cannot be reopened' USING ERRCODE = '23514';
    END IF;
    IF OLD.published_version_id IS NOT NULL
       AND ROW(NEW.published_version_id,NEW.published_at) IS DISTINCT FROM ROW(OLD.published_version_id,OLD.published_at) THEN
        RAISE EXCEPTION 'Publication result is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.review_status IS NULL AND NEW.review_status IS NOT NULL THEN
        IF NEW.review_status <> 'pending' OR NEW.submission_no <> 1 THEN
            RAISE EXCEPTION 'First submission must enter pending round one' USING ERRCODE = '23514';
        END IF;
    ELSIF OLD.review_status = 'rejected' AND NEW.review_status IS DISTINCT FROM 'rejected' THEN
        IF NEW.review_status IS DISTINCT FROM 'pending' OR NEW.submission_no <> OLD.submission_no + 1 THEN
            RAISE EXCEPTION 'Resubmission must enter the next pending round' USING ERRCODE = '23514';
        END IF;
    ELSIF OLD.review_status = 'pending' AND NEW.review_status IS NULL THEN
        RAISE EXCEPTION 'Pending submission cannot become a draft' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_restrict_publication_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE found boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_reviews WHERE resource_type=$1 AND publication_id=$2)',
                   TG_TABLE_SCHEMA) INTO found USING TG_ARGV[0], OLD.id;
    IF found THEN
        RAISE EXCEPTION 'Reviewed publication cannot be deleted' USING ERRCODE = '23503';
    END IF;
    RETURN OLD;
END;
$$;

CREATE FUNCTION aih_immutable_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Published versions and review history are immutable' USING ERRCODE = '23514';
END;
$$;

CREATE FUNCTION aih_immutable_resource_id() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id <> OLD.id THEN
        RAISE EXCEPTION 'Resource identity cannot be changed' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
