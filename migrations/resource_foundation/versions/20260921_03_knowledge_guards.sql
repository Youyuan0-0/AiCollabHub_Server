-- 本地文档处理的状态不属于资源概览版本；工作进程回写必须带任务和租约条件。
CREATE FUNCTION aih_guard_knowledge_index_state() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE changed boolean; active boolean;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 THEN RAISE EXCEPTION 'Initial configuration revision is one' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END IF;
    IF ROW(NEW.id,NEW.knowledge_base_id,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.knowledge_base_id,OLD.created_at) THEN
        RAISE EXCEPTION 'Index state ownership is immutable' USING ERRCODE='23514';
    END IF;
    changed:=aih_index_configuration(to_jsonb(NEW)) IS DISTINCT FROM aih_index_configuration(to_jsonb(OLD));
    IF NEW.revision<>OLD.revision+(CASE WHEN changed THEN 1 ELSE 0 END) THEN
        RAISE EXCEPTION 'Only configuration changes increment its revision' USING ERRCODE='23514';
    END IF;
    IF changed THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.knowledge_index_jobs WHERE knowledge_base_id=$1 AND status IN (''queued'',''running''))',TG_TABLE_SCHEMA)
            INTO active USING NEW.knowledge_base_id;
        IF active THEN RAISE EXCEPTION 'Finish or cancel the active index job before changing configuration' USING ERRCODE='23514'; END IF;
        IF ROW(NEW.mode,NEW.embedding_model,NEW.chunk_mode,NEW.chunk_size,NEW.chunk_overlap) IS DISTINCT FROM
           ROW(OLD.mode,OLD.embedding_model,OLD.chunk_mode,OLD.chunk_size,OLD.chunk_overlap) THEN
            NEW.needs_rebuild:=true;
        END IF;
    END IF;
    IF NEW.retrieval_count<OLD.retrieval_count OR NEW.retrieval_success_count<OLD.retrieval_success_count
       OR NEW.retrieval_elapsed_ms<OLD.retrieval_elapsed_ms THEN
        RAISE EXCEPTION 'Runtime counters must be accumulated, not overwritten with stale values' USING ERRCODE='23514';
    END IF;
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_knowledge_document() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE file_owner uuid; job record;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Soft-delete knowledge documents to preserve review and file history' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.review_status<>'pending' OR NEW.submission_no<>1 OR NEW.revision<>1
           OR NEW.processing_status<>'awaiting_review' OR NEW.index_job_id IS NOT NULL OR NEW.deleted_at IS NOT NULL THEN
            RAISE EXCEPTION 'New documents must start awaiting review' USING ERRCODE='23514';
        END IF;
        EXECUTE format('SELECT uploader_id FROM %I.files WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
            INTO file_owner USING NEW.file_id;
        IF file_owner IS DISTINCT FROM NEW.uploader_id THEN
            RAISE EXCEPTION 'Document uploader must own the source file' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END IF;
    IF ROW(NEW.id,NEW.knowledge_base_id,NEW.file_id,NEW.name,NEW.uploader_id,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.knowledge_base_id,OLD.file_id,OLD.name,OLD.uploader_id,OLD.created_at) THEN
        RAISE EXCEPTION 'Replace a file by submitting a new document, not overwriting review history' USING ERRCODE='23514';
    END IF;
    IF OLD.deleted_at IS NOT NULL OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'Deleted or stale document update' USING ERRCODE='23514';
    END IF;
    IF OLD.review_status IN ('rejected','needs_changes') AND NEW.review_status='pending' THEN
        IF NEW.submission_no<>OLD.submission_no+1 OR NEW.submitted_at<OLD.submitted_at THEN
            RAISE EXCEPTION 'Resubmission needs the next round and a valid submission time' USING ERRCODE='23514';
        END IF;
    ELSIF NEW.submission_no<>OLD.submission_no OR NEW.submitted_at<>OLD.submitted_at OR
          (NEW.review_status<>OLD.review_status AND OLD.review_status<>'pending') THEN
        RAISE EXCEPTION 'Invalid document review transition' USING ERRCODE='23514';
    END IF;
    IF NEW.index_job_id IS DISTINCT FROM OLD.index_job_id OR
       (NEW.processing_status IS DISTINCT FROM OLD.processing_status AND NEW.processing_status IN ('parsing','ready','failed')) THEN
        EXECUTE format('SELECT * FROM %I.knowledge_index_jobs WHERE id=$1 FOR SHARE',TG_TABLE_SCHEMA)
            INTO job USING NEW.index_job_id;
        IF job.id IS NULL OR job.knowledge_base_id<>NEW.knowledge_base_id OR NOT NEW.id=ANY(job.document_ids)
           OR job.status<>'running' OR job.lease_expires_at<=clock_timestamp() THEN
            RAISE EXCEPTION 'Document update needs its current live index job' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.processing_status='ready' AND NEW.processed_at IS NULL THEN
        RAISE EXCEPTION 'Ready document needs its processing completion time' USING ERRCODE='23514';
    END IF;
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_knowledge_document_review() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc record; current_version uuid;
BEGIN
    EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
        INTO doc USING NEW.document_id;
    IF doc.id IS NULL THEN RAISE EXCEPTION 'Document is missing' USING ERRCODE='23503'; END IF;
    IF doc.deleted_at IS NOT NULL OR doc.review_status<>'pending' OR NEW.submission_no<>doc.submission_no
       OR NEW.submitted_at<>doc.submitted_at THEN
        RAISE EXCEPTION 'Review must match the pending document round' USING ERRCODE='23514';
    END IF;
    IF NEW.submission_snapshot<>jsonb_build_object('documentId',doc.id,'fileId',doc.file_id,'name',doc.name) THEN
        RAISE EXCEPTION 'Document review snapshot does not match the submitted file' USING ERRCODE='23514';
    END IF;
    IF NEW.decision='approved' THEN
        EXECUTE format('SELECT current_version_id FROM %I.knowledge_bases WHERE id=$1 FOR KEY SHARE',TG_TABLE_SCHEMA)
            INTO current_version USING doc.knowledge_base_id;
        IF current_version IS NULL THEN
            RAISE EXCEPTION 'Publish an approved knowledge base before approving documents' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_knowledge_document_review() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc record; reviewed record; document_id uuid;
BEGIN
    IF TG_TABLE_NAME='knowledge_documents' THEN document_id:=NEW.id; ELSE document_id:=NEW.document_id; END IF;
    EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=$1',TG_TABLE_SCHEMA) INTO doc USING document_id;
    IF doc.id IS NULL THEN RETURN NULL; END IF;
    EXECUTE format('SELECT * FROM %I.knowledge_document_reviews WHERE document_id=$1 AND submission_no=$2',TG_TABLE_SCHEMA)
        INTO reviewed USING doc.id,doc.submission_no;
    IF (doc.review_status='pending' AND reviewed.id IS NOT NULL) OR
       (doc.review_status<>'pending' AND (reviewed.id IS NULL OR reviewed.decision<>doc.review_status)) THEN
        RAISE EXCEPTION 'Document state and immutable review must commit together' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_guard_knowledge_index_job() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE configuration record; current_version uuid; item record; found_count integer:=0; incomplete boolean;
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'queued' THEN RAISE EXCEPTION 'Index jobs start queued' USING ERRCODE='23514'; END IF;
        EXECUTE format('SELECT * FROM %I.knowledge_index_states WHERE knowledge_base_id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
            INTO configuration USING NEW.knowledge_base_id;
        IF configuration.id IS NULL OR configuration.revision<>NEW.configuration_revision OR
           NEW.configuration_snapshot<>aih_index_configuration(to_jsonb(configuration)) THEN
            RAISE EXCEPTION 'Index job configuration is missing or stale' USING ERRCODE='23514';
        END IF;
        EXECUTE format('SELECT current_version_id FROM %I.knowledge_bases WHERE id=$1 FOR KEY SHARE',TG_TABLE_SCHEMA)
            INTO current_version USING NEW.knowledge_base_id;
        IF current_version IS NULL OR current_version<>NEW.knowledge_base_version_id THEN
            RAISE EXCEPTION 'Index job requires the current approved knowledge base version' USING ERRCODE='23514';
        END IF;
        FOR item IN EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=ANY($1) ORDER BY id FOR UPDATE',TG_TABLE_SCHEMA)
                    USING NEW.document_ids LOOP
            found_count:=found_count+1;
            IF item.knowledge_base_id<>NEW.knowledge_base_id OR item.deleted_at IS NOT NULL OR item.review_status<>'approved' THEN
                RAISE EXCEPTION 'Index job documents must be approved and belong to this base' USING ERRCODE='23514';
            END IF;
        END LOOP;
        IF found_count<>cardinality(NEW.document_ids) THEN
            RAISE EXCEPTION 'Index job has missing or duplicated documents' USING ERRCODE='23503';
        END IF;
        RETURN NEW;
    END IF;
    IF ROW(NEW.id,NEW.knowledge_base_id,NEW.knowledge_base_version_id,NEW.configuration_revision,
           NEW.configuration_snapshot,NEW.document_ids,NEW.mode,NEW.requester_id,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.knowledge_base_id,OLD.knowledge_base_version_id,OLD.configuration_revision,
           OLD.configuration_snapshot,OLD.document_ids,OLD.mode,OLD.requester_id,OLD.created_at) THEN
        RAISE EXCEPTION 'Index job input is immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.status IN ('completed','failed','cancelled') OR
       (OLD.status='running' AND NEW.status='queued') OR
       (OLD.status='queued' AND NEW.status='completed') THEN
        RAISE EXCEPTION 'Invalid index job transition; retries create a new job' USING ERRCODE='23514';
    END IF;
    IF OLD.status='running' THEN
        IF NEW.worker_token IS DISTINCT FROM OLD.worker_token OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION 'A running job cannot change worker ownership' USING ERRCODE='23514';
        END IF;
        IF OLD.lease_expires_at<=clock_timestamp() AND NEW.status IN ('running','completed') THEN
            RAISE EXCEPTION 'Expired worker cannot renew or complete the old job' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.status='completed' THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.knowledge_documents WHERE id=ANY($1) '
            'AND (index_job_id IS DISTINCT FROM $2 OR processing_status<>''ready'' OR deleted_at IS NOT NULL))',TG_TABLE_SCHEMA)
            INTO incomplete USING NEW.document_ids,NEW.id;
        IF incomplete THEN RAISE EXCEPTION 'All selected documents must finish before the job completes' USING ERRCODE='23514'; END IF;
    END IF;
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_knowledge_document_result() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc record; generated boolean;
BEGIN
    EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=$1',TG_TABLE_SCHEMA) INTO doc USING NEW.id;
    IF doc.id IS NULL OR doc.deleted_at IS NOT NULL OR doc.processing_status<>'ready' THEN RETURN NULL; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.knowledge_document_chunks WHERE document_id=$1 AND index_job_id=$2)',TG_TABLE_SCHEMA)
        INTO generated USING doc.id,doc.index_job_id;
    IF NOT generated THEN
        RAISE EXCEPTION 'Ready document requires a generated chunk from its current job' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_guard_knowledge_chunk() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc record; job record;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'Rebuild a chunk instead of rewriting generated content' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN
        EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
            INTO doc USING OLD.document_id;
        IF doc.processing_status='ready' AND doc.deleted_at IS NULL THEN
            RAISE EXCEPTION 'Invalidate the document before removing served chunks' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    EXECUTE format('SELECT * FROM %I.knowledge_documents WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
        INTO doc USING NEW.document_id;
    EXECUTE format('SELECT * FROM %I.knowledge_index_jobs WHERE id=$1 FOR SHARE',TG_TABLE_SCHEMA)
        INTO job USING NEW.index_job_id;
    IF doc.id IS NULL OR job.id IS NULL THEN RAISE EXCEPTION 'Document or job is missing' USING ERRCODE='23503'; END IF;
    IF doc.knowledge_base_id<>NEW.knowledge_base_id OR job.knowledge_base_id<>NEW.knowledge_base_id OR
       doc.index_job_id IS DISTINCT FROM job.id OR NOT doc.id=ANY(job.document_ids) OR
       doc.deleted_at IS NOT NULL OR doc.review_status<>'approved' OR doc.processing_status<>'parsing' OR
       job.status<>'running' OR job.lease_expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'Chunk needs an approved document and its live processing job' USING ERRCODE='23514';
    END IF;
    IF (job.configuration_snapshot->>'mode'='high_quality' AND NEW.embedding IS NULL) OR
       (job.configuration_snapshot->>'mode'='economic' AND NEW.embedding IS NOT NULL) THEN
        RAISE EXCEPTION 'Chunk embedding must match the actual index mode' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER guard_knowledge_index_state BEFORE INSERT OR UPDATE ON knowledge_index_states
FOR EACH ROW EXECUTE FUNCTION aih_guard_knowledge_index_state();
CREATE TRIGGER guard_knowledge_document BEFORE INSERT OR UPDATE OR DELETE ON knowledge_documents
FOR EACH ROW EXECUTE FUNCTION aih_guard_knowledge_document();
CREATE TRIGGER reference_ready_file BEFORE INSERT ON knowledge_documents
FOR EACH ROW EXECUTE FUNCTION aih_reference_ready_file();
CREATE CONSTRAINT TRIGGER check_document_review AFTER INSERT OR UPDATE ON knowledge_documents
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_knowledge_document_review();
CREATE CONSTRAINT TRIGGER check_document_result AFTER UPDATE ON knowledge_documents
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_knowledge_document_result();
CREATE TRIGGER guard_document_review BEFORE INSERT ON knowledge_document_reviews
FOR EACH ROW EXECUTE FUNCTION aih_guard_knowledge_document_review();
CREATE CONSTRAINT TRIGGER check_document_review AFTER INSERT ON knowledge_document_reviews
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_knowledge_document_review();
CREATE TRIGGER immutable_document_review BEFORE UPDATE OR DELETE ON knowledge_document_reviews
FOR EACH ROW EXECUTE FUNCTION aih_immutable_business_history();
CREATE TRIGGER guard_knowledge_index_job BEFORE INSERT OR UPDATE ON knowledge_index_jobs
FOR EACH ROW EXECUTE FUNCTION aih_guard_knowledge_index_job();
CREATE TRIGGER retain_knowledge_index_job BEFORE DELETE ON knowledge_index_jobs
FOR EACH ROW EXECUTE FUNCTION aih_immutable_business_history();
CREATE TRIGGER guard_knowledge_chunk BEFORE INSERT OR UPDATE OR DELETE ON knowledge_document_chunks
FOR EACH ROW EXECUTE FUNCTION aih_guard_knowledge_chunk();
