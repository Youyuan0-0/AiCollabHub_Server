-- 消息、附件及用户归属在数据库保持一致；HTTP 权限仍由业务接口校验。
CREATE FUNCTION aih_guard_conversation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id,NEW.user_id,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.user_id,OLD.created_at) THEN
        RAISE EXCEPTION 'Conversation ownership is immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.deleted_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Deleted conversation cannot be restored or changed' USING ERRCODE='23514';
    END IF;
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_message() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE conversation record; question record; entry record;
BEGIN
    IF TG_OP='DELETE' THEN entry:=OLD; ELSE entry:=NEW; END IF;
    EXECUTE format('SELECT * FROM %I.conversations WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
        INTO conversation USING entry.conversation_id;
    IF conversation.id IS NULL THEN
        RAISE EXCEPTION 'Conversation does not exist' USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN
        IF conversation.deleted_at IS NULL THEN
            RAISE EXCEPTION 'Delete the conversation before retention cleanup' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    IF conversation.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'Deleted conversation cannot accept messages' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF ROW(NEW.id,NEW.conversation_id,NEW.sequence_no,NEW.role,NEW.reply_to_id,NEW.idempotency_key,
               NEW.created_at,NEW.context_snapshot-'actualModelName') IS DISTINCT FROM
           ROW(OLD.id,OLD.conversation_id,OLD.sequence_no,OLD.role,OLD.reply_to_id,OLD.idempotency_key,
               OLD.created_at,OLD.context_snapshot-'actualModelName') THEN
            RAISE EXCEPTION 'Sent message identity and request context are immutable' USING ERRCODE='23514';
        END IF;
        IF OLD.role='user' OR OLD.status IN ('completed','failed','cancelled') THEN
            RAISE EXCEPTION 'Final messages cannot be overwritten' USING ERRCODE='23514';
        END IF;
        IF OLD.status='unknown' AND NEW.status='pending' THEN
            RAISE EXCEPTION 'Unknown upstream result cannot be blindly retried' USING ERRCODE='23514';
        END IF;
        NEW.updated_at:=statement_timestamp();
    END IF;
    IF NEW.role='assistant' THEN
        EXECUTE format('SELECT * FROM %I.conversation_messages WHERE id=$1',TG_TABLE_SCHEMA)
            INTO question USING NEW.reply_to_id;
        IF question.id IS NULL OR question.role<>'user' OR question.conversation_id<>NEW.conversation_id
           OR question.sequence_no>=NEW.sequence_no THEN
            RAISE EXCEPTION 'Answer must follow a user question in the same conversation' USING ERRCODE='23514';
        END IF;
    END IF;
    EXECUTE format('UPDATE %I.conversations SET updated_at=statement_timestamp() WHERE id=$1',TG_TABLE_SCHEMA)
        USING NEW.conversation_id;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_check_question_content() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item record; attached boolean;
BEGIN
    EXECUTE format('SELECT * FROM %I.conversation_messages WHERE id=$1',TG_TABLE_SCHEMA) INTO item USING NEW.id;
    IF item.id IS NULL OR item.role<>'user' OR trim(item.content)<>'' THEN RETURN NULL; END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.conversation_message_attachments WHERE message_id=$1)',TG_TABLE_SCHEMA)
        INTO attached USING item.id;
    IF NOT attached THEN
        RAISE EXCEPTION 'Question needs text or a ready attachment' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE FUNCTION aih_guard_message_attachment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE item record; conversation record; file_owner uuid; answered boolean; entry record;
BEGIN
    IF TG_OP='UPDATE' THEN
        RAISE EXCEPTION 'Sent attachments cannot be replaced' USING ERRCODE='23514';
    END IF;
    IF TG_OP='DELETE' THEN entry:=OLD; ELSE entry:=NEW; END IF;
    EXECUTE format('SELECT * FROM %I.conversation_messages WHERE id=$1',TG_TABLE_SCHEMA) INTO item USING entry.message_id;
    IF item.id IS NULL THEN RAISE EXCEPTION 'Message is missing' USING ERRCODE='23503'; END IF;
    EXECUTE format('SELECT * FROM %I.conversations WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
        INTO conversation USING item.conversation_id;
    IF TG_OP='DELETE' THEN
        IF conversation.deleted_at IS NULL THEN
            RAISE EXCEPTION 'Sent attachments are retained with conversation history' USING ERRCODE='23514';
        END IF;
        RETURN OLD;
    END IF;
    IF item.role<>'user' OR conversation.deleted_at IS NOT NULL THEN
        RAISE EXCEPTION 'Only an active user question accepts attachments' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.conversation_messages WHERE reply_to_id=$1)',TG_TABLE_SCHEMA)
        INTO answered USING item.id;
    IF answered THEN
        RAISE EXCEPTION 'Attach files before creating the answer placeholder' USING ERRCODE='23514';
    END IF;
    EXECUTE format('SELECT uploader_id FROM %I.files WHERE id=$1 FOR UPDATE',TG_TABLE_SCHEMA)
        INTO file_owner USING NEW.file_id;
    IF file_owner IS DISTINCT FROM conversation.user_id THEN
        RAISE EXCEPTION 'Question attachment belongs to another user' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_announcement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.id,NEW.creator_id,NEW.created_at) IS DISTINCT FROM ROW(OLD.id,OLD.creator_id,OLD.created_at) THEN
        RAISE EXCEPTION 'Announcement identity is immutable' USING ERRCODE='23514';
    END IF;
    IF OLD.status='published' AND (NEW.status NOT IN ('published','withdrawn') OR
       ROW(NEW.title,NEW.summary,NEW.content_markdown,NEW.published_at) IS DISTINCT FROM
       ROW(OLD.title,OLD.summary,OLD.content_markdown,OLD.published_at)) THEN
        RAISE EXCEPTION 'Withdraw a published announcement before editing' USING ERRCODE='23514';
    END IF;
    NEW.updated_at:=statement_timestamp();
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_restrict_home_resource_delete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE found boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.home_recommendations WHERE resource_type=$1 AND resource_id=$2)',TG_TABLE_SCHEMA)
        INTO found USING TG_ARGV[0],OLD.id;
    IF found THEN RAISE EXCEPTION 'Resource is referenced on the home page' USING ERRCODE='23503'; END IF;
    RETURN OLD;
END;
$$;

-- 追加保护而非重写前两轮文件规则，保留反馈、下载、审核和预览的既有约束。
CREATE FUNCTION aih_home_knowledge_file_used(schema_name text,file_id uuid) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE used boolean;
BEGIN
    EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.conversation_message_attachments WHERE file_id=$1) '
                   'OR EXISTS (SELECT 1 FROM %I.knowledge_documents WHERE file_id=$1)',schema_name,schema_name)
        INTO used USING file_id;
    RETURN used;
END;
$$;

CREATE FUNCTION aih_guard_home_knowledge_file() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' THEN
        IF NEW.status NOT IN ('deleting','deleted') AND NEW.expires_at IS NULL THEN RETURN NEW; END IF;
    END IF;
    IF aih_home_knowledge_file_used(TG_TABLE_SCHEMA,OLD.id) THEN
        RAISE EXCEPTION 'Conversation or knowledge history still references this file' USING ERRCODE='23503';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION aih_guard_home_knowledge_preview() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- guard_file_preview 已先按 UUID 排序锁定两个文件，此处沿用其锁扩展检查。
    IF TG_OP='DELETE' THEN
        IF aih_home_knowledge_file_used(TG_TABLE_SCHEMA,OLD.source_file_id) OR
           aih_home_knowledge_file_used(TG_TABLE_SCHEMA,OLD.preview_file_id) THEN
            RAISE EXCEPTION 'Referenced preview is retained with home or knowledge files' USING ERRCODE='23503';
        END IF;
        RETURN OLD;
    END IF;
    IF aih_home_knowledge_file_used(TG_TABLE_SCHEMA,NEW.preview_file_id) THEN
        RAISE EXCEPTION 'An independently used file cannot become a preview' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER guard_conversation BEFORE UPDATE ON conversations
FOR EACH ROW EXECUTE FUNCTION aih_guard_conversation();
CREATE TRIGGER guard_message BEFORE INSERT OR UPDATE OR DELETE ON conversation_messages
FOR EACH ROW EXECUTE FUNCTION aih_guard_message();
CREATE CONSTRAINT TRIGGER check_question_content AFTER INSERT OR UPDATE ON conversation_messages
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_question_content();
CREATE TRIGGER guard_message_attachment BEFORE INSERT OR UPDATE OR DELETE ON conversation_message_attachments
FOR EACH ROW EXECUTE FUNCTION aih_guard_message_attachment();
CREATE TRIGGER reference_ready_file BEFORE INSERT ON conversation_message_attachments
FOR EACH ROW EXECUTE FUNCTION aih_reference_ready_file();
CREATE TRIGGER guard_announcement BEFORE UPDATE ON announcements
FOR EACH ROW EXECUTE FUNCTION aih_guard_announcement();
CREATE TRIGGER check_resource_reference BEFORE INSERT OR UPDATE ON home_recommendations
FOR EACH ROW WHEN (NEW.resource_id IS NOT NULL) EXECUTE FUNCTION aih_check_resource_reference();
CREATE TRIGGER guard_home_knowledge_file BEFORE UPDATE OR DELETE ON files
FOR EACH ROW EXECUTE FUNCTION aih_guard_home_knowledge_file();
CREATE TRIGGER guard_home_knowledge_preview BEFORE INSERT OR DELETE ON file_previews
FOR EACH ROW EXECUTE FUNCTION aih_guard_home_knowledge_preview();
DO $$
DECLARE item record;
BEGIN
    FOR item IN SELECT * FROM (VALUES ('dataset','datasets'),('model','models'),
       ('application','applications'),('knowledge','knowledge_bases'),('tool','ai_tools')) AS t(kind,name) LOOP
        EXECUTE format('CREATE TRIGGER restrict_home_resource_delete BEFORE DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION aih_restrict_home_resource_delete(%L)',item.name,item.kind);
    END LOOP;
END;
$$;
