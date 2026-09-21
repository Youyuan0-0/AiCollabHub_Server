-- 本次迁移使用的固定结构校验，不依赖应用进程或可变类型模型。
CREATE FUNCTION aih_valid_message_context(value jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE key text;
BEGIN
    IF jsonb_typeof(value)<>'object' OR octet_length(value::text)>16384 THEN RETURN false; END IF;
    IF value - ARRAY['requestedModelId','requestedModelName','knowledgeBaseId','knowledgeBaseName',
                     'capabilities','actualModelName'] <> '{}'::jsonb THEN RETURN false; END IF;
    IF NOT value ? 'capabilities' OR aih_is_string_array(value->'capabilities') IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    FOREACH key IN ARRAY ARRAY['requestedModelId','requestedModelName','knowledgeBaseId',
                               'knowledgeBaseName','actualModelName'] LOOP
        IF value ? key AND jsonb_typeof(value->key) NOT IN ('null','string') THEN RETURN false; END IF;
    END LOOP;
    FOREACH key IN ARRAY ARRAY['requestedModelId','knowledgeBaseId'] LOOP
        IF value->>key IS NOT NULL THEN PERFORM (value->>key)::uuid; END IF;
    END LOOP;
    RETURN true;
EXCEPTION WHEN invalid_text_representation THEN RETURN false;
END;
$$;

CREATE FUNCTION aih_valid_message_sources(value jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE item jsonb;
BEGIN
    IF jsonb_typeof(value)<>'array' OR octet_length(value::text)>65536 THEN RETURN false; END IF;
    IF jsonb_array_length(value)>20 THEN RETURN false; END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(value) LOOP
        IF jsonb_typeof(item)<>'object' OR item - ARRAY['title','location','score','resourceType',
                'resourceId','externalDocumentId'] <> '{}'::jsonb THEN RETURN false; END IF;
        IF jsonb_typeof(item->'title') IS DISTINCT FROM 'string' OR trim(item->>'title')='' THEN
            RETURN false;
        END IF;
        IF item ? 'location' AND jsonb_typeof(item->'location') NOT IN ('string','null') THEN RETURN false; END IF;
        IF item ? 'externalDocumentId' AND jsonb_typeof(item->'externalDocumentId') NOT IN ('string','null') THEN RETURN false; END IF;
        IF item->>'resourceType' IS NOT NULL AND item->>'resourceType' NOT IN ('dataset','model','application','knowledge','tool') THEN RETURN false; END IF;
        IF item->>'resourceId' IS NOT NULL THEN PERFORM (item->>'resourceId')::uuid; END IF;
        IF item ? 'score' AND item->'score'<>'null'::jsonb THEN
            IF jsonb_typeof(item->'score')<>'number' THEN RETURN false; END IF;
            IF (item->>'score')::numeric NOT BETWEEN 0 AND 1 THEN RETURN false; END IF;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION WHEN invalid_text_representation THEN RETURN false;
END;
$$;

CREATE FUNCTION aih_valid_document_ids(value uuid[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN coalesce(array_ndims(value),1)<>1 THEN false ELSE
        cardinality(value)<=500 AND array_position(value,NULL) IS NULL
        AND cardinality(value)=(SELECT count(DISTINCT item) FROM unnest(value) item) END;
$$;

CREATE FUNCTION aih_valid_embedding(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN jsonb_typeof(value)<>'array' THEN false ELSE
        jsonb_array_length(value)>0 AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(value) item WHERE jsonb_typeof(item)<>'number'
        ) END;
$$;

CREATE FUNCTION aih_index_configuration(value jsonb) RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT jsonb_build_object(
        'mode',value->'mode','embeddingModel',value->'embedding_model',
        'chunkMode',value->'chunk_mode','chunkSize',value->'chunk_size',
        'chunkOverlap',value->'chunk_overlap','retrievalMode',value->'retrieval_mode',
        'topK',value->'top_k','similarityThreshold',value->'similarity_threshold');
$$;
