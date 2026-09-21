-- 随本次追加迁移冻结；服务负责可信身份、配置白名单及业务 JSON 的文件提取。
CREATE FUNCTION aih_guard_module_setting() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 THEN
            RAISE EXCEPTION 'Settings must start at revision one' USING ERRCODE='23514';
        END IF;
    ELSE
        IF TG_TABLE_NAME='module_configurations' THEN
            IF ROW(NEW.id,NEW.module_code,NEW.config_key,NEW.created_at)
               IS DISTINCT FROM ROW(OLD.id,OLD.module_code,OLD.config_key,OLD.created_at) THEN
                RAISE EXCEPTION 'Configuration identity is immutable' USING ERRCODE='23514';
            END IF;
        ELSE
            IF ROW(NEW.id,NEW.resource_type,NEW.resource_id,NEW.created_at)
               IS DISTINCT FROM ROW(OLD.id,OLD.resource_type,OLD.resource_id,OLD.created_at) THEN
                RAISE EXCEPTION 'Visibility identity is immutable' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.revision<>OLD.revision+1 THEN
            RAISE EXCEPTION 'Settings revision must increase by one' USING ERRCODE='23514';
        END IF;
        NEW.updated_at := statement_timestamp();
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_lock_visibility_target() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier uuid; found boolean;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'Replace visibility targets by deleting and adding users' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN identifier:=OLD.policy_id; ELSE identifier:=NEW.policy_id; END IF;
    -- 所有清单修改锁同一规则；业务服务还须匹配并递增规则修订号。
    EXECUTE format('SELECT true FROM %I.resource_visibility_policies WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO found USING identifier;
    IF found IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Visibility policy does not exist' USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_visibility_targets() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier uuid; policy_scope text; has_targets boolean;
BEGIN
    IF TG_TABLE_NAME='resource_visibility_policies' THEN identifier:=NEW.id;
    ELSIF TG_OP='DELETE' THEN identifier:=OLD.policy_id;
    ELSE identifier:=NEW.policy_id; END IF;
    -- 以事务最终结果检查，允许先切换范围再整体替换清单。
    EXECUTE format('SELECT scope FROM %I.resource_visibility_policies WHERE id=$1', TG_TABLE_SCHEMA)
    INTO policy_scope USING identifier;
    IF policy_scope IS NULL THEN RETURN NULL; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_visibility_targets WHERE policy_id=$1)', TG_TABLE_SCHEMA)
    INTO has_targets USING identifier;
    IF (policy_scope='members') IS DISTINCT FROM has_targets THEN
        RAISE EXCEPTION 'Members scope requires targets; all scope forbids targets' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION aih_restrict_resource_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE child text; found boolean;
BEGIN
    FOREACH child IN ARRAY ARRAY['resource_favorites','resource_follows','resource_ratings','resource_downloads','resource_visibility_policies'] LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE resource_type=$1 AND resource_id=$2)',
                       TG_TABLE_SCHEMA, child) INTO found USING TG_ARGV[0], OLD.id;
        IF found THEN
            RAISE EXCEPTION 'Resource still has interactions or visibility policy' USING ERRCODE='23503';
        END IF;
    END LOOP;
    RETURN OLD;
END;
$$;

CREATE OR REPLACE FUNCTION aih_restrict_publication_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE found boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_reviews WHERE resource_type=$1 AND publication_id=$2) '
                   'OR EXISTS (SELECT 1 FROM %I.resource_file_references WHERE resource_type=$1 AND owner_type=''publication'' AND owner_id=$2)',
                   TG_TABLE_SCHEMA, TG_TABLE_SCHEMA) INTO found USING TG_ARGV[0], OLD.id;
    IF found THEN
        RAISE EXCEPTION 'Publication still has reviews or file references' USING ERRCODE='23503';
    END IF;
    RETURN OLD;
END;
$$;

CREATE FUNCTION aih_guard_resource_file_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE entry record; target record; publication record; history record; stored_file record;
        found boolean; report_count integer;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'File references cannot be rewritten' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN entry:=OLD; ELSE entry:=NEW; END IF;
    SELECT * INTO target FROM aih_resource_tables(entry.resource_type);
    IF target.prefix IS NULL THEN
        RAISE EXCEPTION 'Unknown resource type' USING ERRCODE='23514';
    END IF;
    IF entry.owner_type='publication' THEN
        EXECUTE format('SELECT * FROM %I.%I WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA, target.prefix || '_publications')
        INTO publication USING entry.owner_id;
    ELSIF entry.owner_type IN ('review','version') THEN
        IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Historical file references cannot be deleted' USING ERRCODE='23514';
        END IF;
        IF entry.owner_type='review' THEN
            EXECUTE format('SELECT * FROM %I.resource_reviews WHERE id=$1 AND resource_type=$2', TG_TABLE_SCHEMA)
            INTO history USING entry.owner_id, entry.resource_type;
        ELSE
            EXECUTE format('SELECT * FROM %I.%I WHERE id=$1', TG_TABLE_SCHEMA, target.prefix || '_versions')
            INTO history USING entry.owner_id;
        END IF;
        IF history.id IS NULL THEN
            RAISE EXCEPTION 'File owner does not exist in this module' USING ERRCODE='23503';
        END IF;
        EXECUTE format('SELECT * FROM %I.%I WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA, target.prefix || '_publications')
        INTO publication USING history.publication_id;
    ELSE
        RAISE EXCEPTION 'Unknown file owner type' USING ERRCODE='23514';
    END IF;
    IF publication.id IS NULL THEN
        RAISE EXCEPTION 'File publication does not exist' USING ERRCODE='23503';
    END IF;
    IF entry.owner_type='publication' AND entry.purpose='tutorial' THEN
        IF publication.review_status IN ('pending','approved') THEN
            RAISE EXCEPTION 'Submitted tutorial references are frozen' USING ERRCODE='23514';
        END IF;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    EXECUTE format('SELECT * FROM %I.files WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO stored_file USING entry.file_id;
    IF stored_file.id IS NULL THEN
        RAISE EXCEPTION 'Referenced file does not exist' USING ERRCODE='23503';
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.file_previews WHERE preview_file_id=$1)', TG_TABLE_SCHEMA)
    INTO found USING entry.file_id;
    IF found THEN
        RAISE EXCEPTION 'Business references must point to the original file' USING ERRCODE='23514';
    END IF;
    IF entry.owner_type='publication' AND entry.purpose='review_report' THEN
        IF publication.review_status IS DISTINCT FROM 'pending'
           OR entry.submission_no IS DISTINCT FROM publication.submission_no
           OR entry.creator_id IS DISTINCT FROM stored_file.uploader_id THEN
            RAISE EXCEPTION 'Report must match pending round and uploader' USING ERRCODE='23514';
        END IF;
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_file_references WHERE purpose=''review_report'' AND file_id=$1)', TG_TABLE_SCHEMA)
        INTO found USING entry.file_id;
        IF found THEN
            RAISE EXCEPTION 'Report file is already staged or used' USING ERRCODE='23514';
        END IF;
        EXECUTE format('SELECT count(*) FROM %I.resource_file_references WHERE resource_type=$1 AND owner_type=''publication'' '
                       'AND owner_id=$2 AND purpose=''review_report'' AND submission_no=$3 AND creator_id=$4', TG_TABLE_SCHEMA)
        INTO report_count USING entry.resource_type, entry.owner_id, entry.submission_no, entry.creator_id;
        IF report_count>=3 THEN
            RAISE EXCEPTION 'At most three reports per reviewer and submission' USING ERRCODE='23514';
        END IF;
    ELSIF entry.owner_type='review' THEN
        -- 先写审核及附件，最后更新申请结论；结束后禁止追加历史附件。
        IF publication.review_status IS DISTINCT FROM 'pending'
           OR publication.submission_no<>history.submission_no THEN
            RAISE EXCEPTION 'Review attachments must be archived before concluding the submission' USING ERRCODE='23514';
        END IF;
        IF entry.purpose='review_report' THEN
            IF entry.creator_id<>history.reviewer_id THEN
                RAISE EXCEPTION 'Report owner must be the reviewing user' USING ERRCODE='23514';
            END IF;
            EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_file_references WHERE resource_type=$1 '
                           'AND owner_type=''publication'' AND owner_id=$2 AND file_id=$3 '
                           'AND purpose=''review_report'' AND submission_no=$4 AND creator_id=$5)', TG_TABLE_SCHEMA)
            INTO found USING entry.resource_type, publication.id, entry.file_id, history.submission_no, history.reviewer_id;
            IF NOT found THEN
                RAISE EXCEPTION 'Review report has no matching staged upload' USING ERRCODE='23514';
            END IF;
        END IF;
    ELSIF entry.owner_type='version' THEN
        -- 版本资料与文件引用写完后再回填申请发布结果，在同一事务提交。
        IF publication.review_status IS DISTINCT FROM 'approved' OR publication.published_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'Version references must be saved before publishing the version' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_archived_files() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE kind text; owner_kind text; expected uuid[]; actual uuid[]; staged boolean;
BEGIN
    IF TG_TABLE_NAME='resource_reviews' THEN kind:=NEW.resource_type; owner_kind:='review';
    ELSE kind:=TG_ARGV[0]; owner_kind:='version'; END IF;
    -- 从申请引用归档；资料 JSON 到申请引用的提取由模块服务按类型模型完成。
    EXECUTE format('SELECT coalesce(array_agg(file_id ORDER BY file_id),ARRAY[]::uuid[]) FROM %I.resource_file_references '
                   'WHERE resource_type=$1 AND owner_type=''publication'' AND owner_id=$2 AND purpose=''tutorial''', TG_TABLE_SCHEMA)
    INTO expected USING kind, NEW.publication_id;
    EXECUTE format('SELECT coalesce(array_agg(file_id ORDER BY file_id),ARRAY[]::uuid[]) FROM %I.resource_file_references '
                   'WHERE resource_type=$1 AND owner_type=$2 AND owner_id=$3 AND purpose=''tutorial''', TG_TABLE_SCHEMA)
    INTO actual USING kind, owner_kind, NEW.id;
    IF actual IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'Historical tutorial references must match submitted references' USING ERRCODE='23514';
    END IF;
    IF owner_kind='review' THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.resource_file_references archived '
                       'JOIN %I.resource_file_references staged ON staged.file_id=archived.file_id '
                       'WHERE archived.owner_type=''review'' AND archived.owner_id=$1 AND archived.purpose=''review_report'' '
                       'AND staged.owner_type=''publication'' AND staged.purpose=''review_report'')', TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
        INTO staged USING NEW.id;
        IF staged THEN
            RAISE EXCEPTION 'Reviewed reports must leave staging in the same transaction' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION aih_guard_file() RETURNS trigger LANGUAGE plpgsql AS $$
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
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.feedback_attachments WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.resource_downloads WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.resource_file_references WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.file_previews WHERE source_file_id=$1 OR preview_file_id=$1)',
                   TG_TABLE_SCHEMA, TG_TABLE_SCHEMA, TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
    INTO referenced USING OLD.id;
    IF referenced THEN
        RAISE EXCEPTION 'Referenced file cannot expire or be deleted' USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION aih_reference_ready_file() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE stored_file record; submitted boolean; is_preview boolean;
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
    EXECUTE format('SELECT * FROM %I.files WHERE id=$1 FOR UPDATE', TG_TABLE_SCHEMA)
    INTO stored_file USING NEW.file_id;
    IF stored_file.id IS NULL THEN
        RAISE EXCEPTION 'File does not exist' USING ERRCODE='23503';
    END IF;
    IF stored_file.status<>'ready' OR stored_file.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'File is not ready or has expired' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.file_previews WHERE preview_file_id=$1)', TG_TABLE_SCHEMA)
    INTO is_preview USING NEW.file_id;
    IF is_preview THEN
        RAISE EXCEPTION 'Business references must point to the original file' USING ERRCODE='23514';
    END IF;
    IF stored_file.expires_at IS NOT NULL THEN
        EXECUTE format('UPDATE %I.files SET expires_at=NULL WHERE id=$1', TG_TABLE_SCHEMA) USING NEW.file_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_file_preview() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE entry record; stored_file record; found boolean; files_count integer:=0;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'Preview mappings cannot be rewritten' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN entry:=OLD; ELSE entry:=NEW; END IF;
    -- 两个文件按编号排序加锁，阻止并发构建链、换向映射及清理竞态。
    FOR stored_file IN EXECUTE format('SELECT * FROM %I.files WHERE id IN ($1,$2) ORDER BY id FOR UPDATE', TG_TABLE_SCHEMA)
                      USING entry.source_file_id, entry.preview_file_id LOOP
        files_count:=files_count+1;
        IF TG_OP='INSERT' THEN
            IF stored_file.status<>'ready' OR stored_file.expires_at<=clock_timestamp() THEN
                RAISE EXCEPTION 'Preview files must be ready and unexpired' USING ERRCODE='23514';
            END IF;
            IF stored_file.expires_at IS NOT NULL THEN
                EXECUTE format('UPDATE %I.files SET expires_at=NULL WHERE id=$1', TG_TABLE_SCHEMA) USING stored_file.id;
            END IF;
        END IF;
    END LOOP;
    IF files_count<>2 THEN
        RAISE EXCEPTION 'Preview requires two different existing files' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.feedback_attachments WHERE file_id IN ($1,$2)) '
                       'OR EXISTS (SELECT 1 FROM %I.resource_downloads WHERE file_id IN ($1,$2)) '
                       'OR EXISTS (SELECT 1 FROM %I.resource_file_references WHERE file_id IN ($1,$2))',
                       TG_TABLE_SCHEMA, TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
        INTO found USING entry.source_file_id, entry.preview_file_id;
        IF found THEN
            RAISE EXCEPTION 'Referenced preview mapping cannot be removed' USING ERRCODE='23503';
        END IF;
        RETURN OLD;
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.file_previews WHERE preview_file_id=$1 OR source_file_id=$2)', TG_TABLE_SCHEMA)
    INTO found USING entry.source_file_id, entry.preview_file_id;
    IF found THEN
        RAISE EXCEPTION 'Preview chains and cycles are forbidden' USING ERRCODE='23514';
    END IF;
    -- 不能把已独立使用的业务文件改成另一个文件的预览，避免改变其权限含义。
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.feedback_attachments WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.resource_downloads WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.resource_file_references WHERE file_id=$1)',
                   TG_TABLE_SCHEMA, TG_TABLE_SCHEMA, TG_TABLE_SCHEMA)
    INTO found USING entry.preview_file_id;
    IF found THEN
        RAISE EXCEPTION 'A referenced business file cannot become a preview' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER guard_module_setting BEFORE INSERT OR UPDATE ON module_configurations
FOR EACH ROW EXECUTE FUNCTION aih_guard_module_setting();
CREATE TRIGGER guard_module_setting BEFORE INSERT OR UPDATE ON resource_visibility_policies
FOR EACH ROW EXECUTE FUNCTION aih_guard_module_setting();
CREATE TRIGGER check_resource_reference BEFORE INSERT OR UPDATE ON resource_visibility_policies
FOR EACH ROW EXECUTE FUNCTION aih_check_resource_reference();
CREATE TRIGGER lock_visibility_target BEFORE INSERT OR UPDATE OR DELETE ON resource_visibility_targets
FOR EACH ROW EXECUTE FUNCTION aih_lock_visibility_target();
CREATE CONSTRAINT TRIGGER check_visibility_targets AFTER INSERT OR UPDATE ON resource_visibility_policies
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_visibility_targets();
CREATE CONSTRAINT TRIGGER check_visibility_targets AFTER INSERT OR UPDATE OR DELETE ON resource_visibility_targets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_visibility_targets();

CREATE TRIGGER guard_resource_file_reference BEFORE INSERT OR UPDATE OR DELETE ON resource_file_references
FOR EACH ROW EXECUTE FUNCTION aih_guard_resource_file_reference();
CREATE TRIGGER reference_ready_file BEFORE INSERT ON resource_file_references
FOR EACH ROW EXECUTE FUNCTION aih_reference_ready_file();
CREATE CONSTRAINT TRIGGER check_archived_files AFTER INSERT ON resource_reviews
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_archived_files();
CREATE TRIGGER guard_file_preview BEFORE INSERT OR UPDATE OR DELETE ON file_previews
FOR EACH ROW EXECUTE FUNCTION aih_guard_file_preview();

DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT kind, m.* FROM unnest(ARRAY['dataset','model','application','knowledge','tool']) kind
                CROSS JOIN LATERAL aih_resource_tables(kind) m LOOP
        EXECUTE format('CREATE CONSTRAINT TRIGGER check_archived_files AFTER INSERT ON %I '
                       'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_archived_files(%L)',
                       item.prefix || '_versions', item.kind);
    END LOOP;
END;
$$;
