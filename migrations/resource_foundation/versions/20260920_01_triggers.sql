-- 触发器补足跨五张主表的多态关联；权限和具体模块内容仍由业务服务验证。
DO $$
DECLARE item record; child text;
BEGIN
    FOREACH child IN ARRAY ARRAY['resource_favorites','resource_follows','resource_ratings','resource_downloads'] LOOP
        EXECUTE format('CREATE TRIGGER check_resource_reference BEFORE INSERT OR UPDATE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_check_resource_reference()', child);
    END LOOP;
    FOR item IN SELECT kind, m.* FROM unnest(ARRAY['dataset','model','application','knowledge','tool']) kind
                CROSS JOIN LATERAL aih_resource_tables(kind) m LOOP
        EXECUTE format('CREATE TRIGGER restrict_resource_delete BEFORE DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_restrict_resource_delete(%L)', item.main_table, item.kind);
        EXECUTE format('CREATE TRIGGER immutable_resource_id BEFORE UPDATE OF id ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_immutable_resource_id()', item.main_table);
        EXECUTE format('CREATE TRIGGER freeze_publication BEFORE UPDATE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_freeze_publication(%L)',
                       item.prefix || '_publications', item.prefix || '_id');
        EXECUTE format('CREATE TRIGGER restrict_publication_delete BEFORE DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_restrict_publication_delete(%L)',
                       item.prefix || '_publications', item.kind);
        EXECUTE format('CREATE CONSTRAINT TRIGGER check_review_outcome AFTER INSERT OR UPDATE ON %I '
                       'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_review_outcome(%L)',
                       item.prefix || '_publications', item.kind);
        EXECUTE format('CREATE CONSTRAINT TRIGGER check_version_source AFTER INSERT ON %I '
                       'DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_version_source(%L)',
                       item.prefix || '_versions', item.prefix || '_publications');
        EXECUTE format('CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION aih_immutable_history()', item.prefix || '_versions');
    END LOOP;
END;
$$;

CREATE TRIGGER check_review_reference BEFORE INSERT ON resource_reviews
FOR EACH ROW EXECUTE FUNCTION aih_check_review_reference();
CREATE CONSTRAINT TRIGGER check_review_outcome AFTER INSERT ON resource_reviews
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aih_check_review_outcome();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON resource_reviews
FOR EACH ROW EXECUTE FUNCTION aih_immutable_history();
