-- 本次迁移只保护已落库关系；后续新增文件引用时须扩展清理检查。
CREATE FUNCTION aih_immutable_business_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Business history cannot be modified or deleted' USING ERRCODE='23514';
END;
$$;

CREATE FUNCTION aih_guard_feedback_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.status<>'processing' THEN
            RAISE EXCEPTION 'Feedback must start in processing revision one' USING ERRCODE='23514';
        END IF;
    ELSE
        IF (to_jsonb(NEW)-ARRAY['status','revision','updated_at','resolved_at'])
           IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','revision','updated_at','resolved_at']) THEN
            RAISE EXCEPTION 'Original feedback is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.revision<>OLD.revision+1 THEN
            RAISE EXCEPTION 'Feedback revision must increase by one' USING ERRCODE='23514';
        END IF;
        IF NEW.status=OLD.status AND NEW.resolved_at IS DISTINCT FROM OLD.resolved_at THEN
            RAISE EXCEPTION 'Reply cannot change resolution time' USING ERRCODE='23514';
        END IF;
        NEW.updated_at := statement_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_lock_feedback_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE found boolean;
BEGIN
    -- 事件和附件都锁同一反馈，避免提交封存与追加附件发生竞态。
    EXECUTE format('SELECT true FROM %I.feedback_records WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO found USING NEW.feedback_id;
    IF found IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Feedback does not exist' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_feedback_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE feedback_id uuid; feedback record; latest record; previous record; first_event record;
BEGIN
    IF TG_TABLE_NAME='feedback_records' THEN feedback_id := NEW.id;
    ELSE feedback_id := NEW.feedback_id; END IF;
    -- 延迟到事务结束，允许主表状态和事件按一个业务事务写入。
    EXECUTE format('SELECT * FROM %I.feedback_records WHERE id=$1', TG_TABLE_SCHEMA)
    INTO feedback USING feedback_id;
    IF feedback.id IS NULL THEN RETURN NULL; END IF;
    EXECUTE format('SELECT * FROM %I.feedback_events WHERE feedback_id=$1 ORDER BY resulting_revision DESC LIMIT 1', TG_TABLE_SCHEMA)
    INTO latest USING feedback_id;
    IF latest.id IS NULL OR latest.resulting_revision<>feedback.revision OR latest.to_status<>feedback.status THEN
        RAISE EXCEPTION 'Feedback and latest event must commit together' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT * FROM %I.feedback_events WHERE feedback_id=$1 AND resulting_revision=1', TG_TABLE_SCHEMA)
    INTO first_event USING feedback_id;
    IF first_event.id IS NULL OR first_event.action<>'submitted'
       OR first_event.actor_id<>feedback.submitter_id OR first_event.idempotency_key<>feedback.idempotency_key THEN
        RAISE EXCEPTION 'Submission event must match feedback owner and request' USING ERRCODE='23514';
    END IF;
    IF TG_TABLE_NAME='feedback_events' THEN
        IF NEW.resulting_revision>1 THEN
            EXECUTE format('SELECT * FROM %I.feedback_events WHERE feedback_id=$1 AND resulting_revision=$2', TG_TABLE_SCHEMA)
            INTO previous USING feedback_id, NEW.resulting_revision-1;
            IF previous.id IS NULL OR previous.to_status IS DISTINCT FROM NEW.from_status THEN
                RAISE EXCEPTION 'Feedback event must continue the previous state and revision' USING ERRCODE='23514';
            END IF;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_guard_file() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE referenced boolean;
BEGIN
    IF TG_OP='UPDATE' THEN
        IF ROW(NEW.id,NEW.uploader_id,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.uploader_id,OLD.created_at) THEN
            RAISE EXCEPTION 'File identity is immutable' USING ERRCODE='23514';
        END IF;
        IF OLD.status IN ('ready','deleting','deleted') AND
           ROW(NEW.file_name,NEW.mime_type,NEW.size_bytes,NEW.bucket,NEW.object_key,NEW.content_hash)
           IS DISTINCT FROM ROW(OLD.file_name,OLD.mime_type,OLD.size_bytes,OLD.bucket,OLD.object_key,OLD.content_hash) THEN
            RAISE EXCEPTION 'Finalized file content and location are immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.status<>OLD.status AND NOT (
            (OLD.status='uploading' AND NEW.status IN ('ready','failed','deleting')) OR
            (OLD.status='failed' AND NEW.status IN ('uploading','deleting')) OR
            (OLD.status='ready' AND NEW.status='deleting') OR
            (OLD.status='deleting' AND NEW.status='deleted')
        ) THEN
            RAISE EXCEPTION 'Invalid file lifecycle transition' USING ERRCODE='23514';
        END IF;
        NEW.updated_at := statement_timestamp();
        IF NEW.status NOT IN ('deleting','deleted') AND NEW.expires_at IS NULL THEN RETURN NEW; END IF;
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.feedback_attachments WHERE file_id=$1) OR EXISTS (SELECT 1 FROM %I.resource_downloads WHERE file_id=$1)', TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
    INTO referenced USING OLD.id;
    IF referenced THEN
        RAISE EXCEPTION 'Referenced file cannot expire or be deleted' USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_reference_ready_file() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE stored_file record; submitted boolean;
BEGIN
    IF TG_TABLE_NAME='feedback_attachments' THEN
        EXECUTE format('SELECT true FROM %I.feedback_records WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
        INTO submitted USING NEW.feedback_id;
        IF submitted IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'Feedback does not exist' USING ERRCODE='23503';
        END IF;
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.feedback_events WHERE feedback_id=$1)', TG_TABLE_SCHEMA)
        INTO submitted USING NEW.feedback_id;
        IF submitted THEN
            RAISE EXCEPTION 'Attachments must be added before the submission event' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NEW.file_id IS NULL THEN RETURN NEW; END IF;
        IF TG_OP='UPDATE' AND NEW.file_id IS NOT DISTINCT FROM OLD.file_id THEN RETURN NEW; END IF;
    END IF;
    -- UPDATE 锁同时保护新增引用和清理状态切换，不只依赖外键的存在性检查。
    EXECUTE format('SELECT * FROM %I.files WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO stored_file USING NEW.file_id;
    IF stored_file.id IS NULL THEN
        RAISE EXCEPTION 'File does not exist' USING ERRCODE='23503';
    END IF;
    IF stored_file.status<>'ready' OR stored_file.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'File is not ready or has expired' USING ERRCODE='23514';
    END IF;
    IF stored_file.expires_at IS NOT NULL THEN
        EXECUTE format('UPDATE %I.files SET expires_at=NULL WHERE id=$1', TG_TABLE_SCHEMA) USING NEW.file_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_application_dependency() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record; found boolean;
BEGIN
    SELECT * INTO target FROM aih_resource_tables(NEW.resource_type);
    IF target.main_table IS NULL OR NEW.resource_type='application' THEN
        RAISE EXCEPTION 'Unsupported application dependency type' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT true FROM %I.%I WHERE id=$1 FOR KEY SHARE', TG_TABLE_SCHEMA, target.main_table)
    INTO found USING NEW.resource_id;
    IF found IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Dependency resource does not exist' USING ERRCODE='23503';
    END IF;
    EXECUTE format('SELECT true FROM %I.%I WHERE id=$1 AND %I=$2 FOR KEY SHARE',
                   TG_TABLE_SCHEMA, target.prefix || '_versions', target.prefix || '_id')
    INTO found USING NEW.resource_version_id, NEW.resource_id;
    IF found IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Dependency version does not belong to resource' USING ERRCODE='23503';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_restrict_dependency_resource_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE referenced boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.application_dependencies WHERE resource_type=$1 AND resource_id=$2)', TG_TABLE_SCHEMA)
    INTO referenced USING TG_ARGV[0], OLD.id;
    IF referenced THEN
        RAISE EXCEPTION 'Resource is referenced by an application version' USING ERRCODE='23503';
    END IF;
    RETURN OLD;
END;
$$;

CREATE FUNCTION aih_dependency_snapshot(value jsonb) RETURNS jsonb LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE result jsonb;
BEGIN
    IF value IS NULL THEN RETURN '[]'::jsonb; END IF;
    IF jsonb_typeof(value)<>'array' THEN
        RAISE EXCEPTION 'Dependency snapshot must be an array' USING ERRCODE='23514';
    END IF;
    IF jsonb_array_length(value)>40 THEN
        RAISE EXCEPTION 'Dependency snapshot must be an array of at most forty items' USING ERRCODE='23514';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object(
        'kind', item->'kind', 'resourceId', item->'resourceId', 'resourceVersionId', item->'resourceVersionId'
    ) ORDER BY position), '[]'::jsonb) INTO result
    FROM jsonb_array_elements(value) WITH ORDINALITY AS items(item,position);
    RETURN result;
END;
$$;

CREATE FUNCTION aih_check_dependency_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE version_id uuid; version record; submitted jsonb; expected jsonb; actual jsonb;
BEGIN
    IF TG_TABLE_NAME='application_versions' THEN version_id := NEW.id;
    ELSE version_id := NEW.version_id; END IF;
    EXECUTE format('SELECT * FROM %I.application_versions WHERE id=$1', TG_TABLE_SCHEMA)
    INTO version USING version_id;
    IF version.id IS NULL THEN
        RAISE EXCEPTION 'Application version does not exist' USING ERRCODE='23503';
    END IF;
    expected := aih_dependency_snapshot(version.snapshot->'dependencies');
    EXECUTE format('SELECT data->''dependencies'' FROM %I.application_publications WHERE id=$1', TG_TABLE_SCHEMA)
    INTO submitted USING version.publication_id;
    IF expected IS DISTINCT FROM aih_dependency_snapshot(submitted) THEN
        RAISE EXCEPTION 'Application dependencies must match the approved submission' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT coalesce(jsonb_agg(jsonb_build_object(''kind'',resource_type,''resourceId'',resource_id::text,''resourceVersionId'',resource_version_id::text) ORDER BY position), ''[]''::jsonb) FROM %I.application_dependencies WHERE version_id=$1', TG_TABLE_SCHEMA)
    INTO actual USING version_id;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'Application dependency rows must match the version snapshot' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER guard_feedback_record BEFORE INSERT OR UPDATE ON feedback_records
FOR EACH ROW EXECUTE FUNCTION aih_guard_feedback_record();
CREATE TRIGGER lock_feedback_event BEFORE INSERT ON feedback_events
FOR EACH ROW EXECUTE FUNCTION aih_lock_feedback_event();
CREATE CONSTRAINT TRIGGER check_feedback_outcome AFTER INSERT OR UPDATE ON feedback_records
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_feedback_outcome();
CREATE CONSTRAINT TRIGGER check_feedback_outcome AFTER INSERT ON feedback_events
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_feedback_outcome();
CREATE TRIGGER immutable_feedback_event BEFORE UPDATE OR DELETE ON feedback_events
FOR EACH ROW EXECUTE FUNCTION aih_immutable_business_history();
CREATE TRIGGER immutable_feedback_attachment BEFORE UPDATE OR DELETE ON feedback_attachments
FOR EACH ROW EXECUTE FUNCTION aih_immutable_business_history();
CREATE TRIGGER reference_ready_file BEFORE INSERT ON feedback_attachments
FOR EACH ROW EXECUTE FUNCTION aih_reference_ready_file();
CREATE TRIGGER reference_ready_file BEFORE INSERT OR UPDATE OF file_id ON resource_downloads
FOR EACH ROW EXECUTE FUNCTION aih_reference_ready_file();
CREATE TRIGGER guard_file BEFORE UPDATE OR DELETE ON files
FOR EACH ROW EXECUTE FUNCTION aih_guard_file();
CREATE TRIGGER check_application_dependency BEFORE INSERT ON application_dependencies
FOR EACH ROW EXECUTE FUNCTION aih_check_application_dependency();
CREATE TRIGGER immutable_application_dependency BEFORE UPDATE OR DELETE ON application_dependencies
FOR EACH ROW EXECUTE FUNCTION aih_immutable_business_history();
CREATE CONSTRAINT TRIGGER check_dependency_snapshot AFTER INSERT ON application_dependencies
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_dependency_snapshot();
CREATE CONSTRAINT TRIGGER check_dependency_snapshot AFTER INSERT ON application_versions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_dependency_snapshot();

DO $$
DECLARE target record;
BEGIN
    FOR target IN SELECT kind,m.* FROM unnest(ARRAY['dataset','model','knowledge','tool']) kind
                  CROSS JOIN LATERAL aih_resource_tables(kind) m LOOP
        EXECUTE format('CREATE TRIGGER restrict_dependency_resource_delete BEFORE DELETE ON %I FOR EACH ROW EXECUTE FUNCTION aih_restrict_dependency_resource_delete(%L)', target.main_table, target.kind);
    END LOOP;
END;
$$;
