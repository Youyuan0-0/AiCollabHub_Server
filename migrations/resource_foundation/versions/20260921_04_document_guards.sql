-- 文档只负责平台上传归属及逐轮审批，不记录供应方的索引任务、分段和向量。
CREATE OR REPLACE FUNCTION aih_guard_knowledge_document() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE file_owner uuid;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Soft-delete knowledge documents to preserve review and file history' USING ERRCODE='23514';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.review_status<>'pending' OR NEW.submission_no<>1 OR NEW.revision<>1 OR NEW.deleted_at IS NOT NULL THEN
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
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;
