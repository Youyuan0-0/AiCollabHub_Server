CREATE OR REPLACE FUNCTION aih_check_review_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
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
    IF publication.review_status IN ('approved','rejected','needs_changes') THEN
        IF review.id IS NULL OR review.decision <> publication.review_status THEN
            RAISE EXCEPTION 'Publication must have a matching review decision' USING ERRCODE = '23514';
        END IF;
    ELSIF review.id IS NOT NULL THEN
        RAISE EXCEPTION 'Review and publication state must commit together' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION aih_freeze_publication() RETURNS trigger LANGUAGE plpgsql AS $$
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
    ELSIF OLD.review_status IN ('rejected','needs_changes')
       AND NEW.review_status IS DISTINCT FROM OLD.review_status THEN
        IF NEW.review_status IS DISTINCT FROM 'pending' OR NEW.submission_no <> OLD.submission_no + 1 THEN
            RAISE EXCEPTION 'Resubmission must enter the next pending round' USING ERRCODE = '23514';
        END IF;
    ELSIF OLD.review_status = 'pending' AND NEW.review_status IS NULL THEN
        RAISE EXCEPTION 'Pending submission cannot become a draft' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
