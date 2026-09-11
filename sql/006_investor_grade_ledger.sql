-- PostgreSQL 17 + pgvector >=0.8.0; apply after 000-005 in one transaction.
-- Canonical physical table rename, not a second ledger. PostgreSQL retains table
-- OIDs, FK targets, indexes, grants and FORCE RLS policies through these renames.
-- Older names remain invoker-security views for granted read compatibility.
-- Administrator fixture inserts can use the automatically updatable views;
-- runtime writers use the explicitly rebound functions below.
-- The audited public API is execute_atomic_sync; internal 005 functions are
-- explicitly rebound below because PL/pgSQL table names are parsed at execution.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hivemind_audit_writer') THEN
    CREATE ROLE hivemind_audit_writer NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO hivemind_audit_writer;
SET LOCAL ROLE hivemind_owner;
ALTER TABLE memory_events RENAME TO immutable_event_log;
ALTER TABLE ledger_constraints RENAME TO authoritative_constraints;
ALTER TABLE vector_knowledge RENAME TO semantic_embeddings;
CREATE VIEW memory_events WITH(security_invoker=true,security_barrier=true)
  AS SELECT * FROM immutable_event_log;
CREATE VIEW ledger_constraints WITH(security_invoker=true,security_barrier=true)
  AS SELECT * FROM authoritative_constraints;
CREATE VIEW vector_knowledge WITH(security_invoker=true,security_barrier=true)
  AS SELECT * FROM semantic_embeddings;
GRANT SELECT ON memory_events,ledger_constraints,vector_knowledge TO hivemind_app,hivemind_ledger_writer,hivemind_jobrunner;
RESET ROLE;

-- Rebind existing function bodies; CREATE OR REPLACE preserves owner/ACL.
CREATE OR REPLACE FUNCTION append_ledger_claim(p_tenant_id uuid,p_project_id uuid,p_event_id uuid,
  p_claim jsonb,p_origin text,p_model text DEFAULT NULL) RETURNS boolean
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE entity text; claim_state text; claim_kind text; digest_value text;
  head public.authoritative_constraints%ROWTYPE; duplicate_head public.authoritative_constraints%ROWTYPE;
  next_version integer; new_ledger_id uuid; new_memory_id uuid; content_value text;
BEGIN
  IF jsonb_typeof(p_claim) IS DISTINCT FROM 'object'
     OR NOT(p_claim ?& ARRAY['entity_key','kind','state','value','expected_version','evidence'])
     OR (p_claim-ARRAY['entity_key','kind','state','value','expected_version','evidence'])<>'{}'::jsonb
     OR jsonb_typeof(p_claim->'entity_key') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_claim->'kind') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_claim->'state') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_claim->'evidence') IS DISTINCT FROM 'string'
     OR jsonb_typeof(p_claim->'expected_version') IS DISTINCT FROM 'number'
     OR (p_claim->>'expected_version') !~ '^[0-9]{1,9}$'
  THEN RAISE EXCEPTION 'invalid_claim_shape' USING ERRCODE='22023'; END IF;
  entity:=lower(btrim(p_claim->>'entity_key')); claim_state:=p_claim->>'state'; claim_kind:=p_claim->>'kind';
  IF entity !~ '^[a-z0-9][a-z0-9_.-]{0,95}$'
     OR claim_state NOT IN ('tentative','accepted','retracted')
     OR claim_kind NOT IN ('fact','constraint','decision','config','task')
     OR octet_length((p_claim->'value')::text)>2048
     OR octet_length(p_claim->>'evidence') NOT BETWEEN 1 AND 2000
     OR p_origin NOT IN ('host','extractor')
     OR (p_origin='extractor' AND (claim_state<>'tentative' OR p_model IS NULL OR length(p_model) NOT BETWEEN 1 AND 120))
  THEN RAISE EXCEPTION 'invalid_claim' USING ERRCODE='22023'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_project_id::text,5));
  SELECT * INTO head FROM public.authoritative_constraints l
    WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity ORDER BY l.version DESC LIMIT 1;
  IF head.id IS NOT NULL AND head.kind<>claim_kind THEN
    RAISE EXCEPTION 'entity_kind_conflict:%',entity USING ERRCODE='23514';
  END IF;
  digest_value:=public.ledger_digest(jsonb_build_object('entity_key',entity,'kind',claim_kind,'state',claim_state,'value',p_claim->'value'));
  -- An exact repeat of the current authority/proposal is a no-op even with a stale
  -- expected_version. Matching an older historical value is NOT a no-op: a revert
  -- is a new transition and must satisfy optimistic locking against the live head.
  SELECT * INTO duplicate_head FROM public.authoritative_constraints l
    WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity
      AND ((claim_state='tentative' AND l.state='tentative') OR (claim_state<>'tentative' AND l.state<>'tentative'))
    ORDER BY l.version DESC LIMIT 1;
  IF duplicate_head.content_digest=digest_value THEN RETURN false; END IF;
  IF COALESCE(head.version,0)<>(p_claim->>'expected_version')::integer THEN
    RAISE EXCEPTION 'ledger_version_conflict:%:current=%',entity,COALESCE(head.version,0) USING ERRCODE='40001';
  END IF;
  IF claim_state='retracted' AND NOT EXISTS(
    SELECT 1 FROM public.authoritative_constraints l WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity
  ) THEN RAISE EXCEPTION 'cannot_retract_missing_entity' USING ERRCODE='23514'; END IF;
  next_version:=COALESCE(head.version,0)+1;
  INSERT INTO public.authoritative_constraints(tenant_id,project_id,entity_key,kind,state,value,version,expected_version,
    content_digest,event_id,evidence,origin,extraction_model)
  VALUES(p_tenant_id,p_project_id,entity,claim_kind,claim_state,public.ledger_canonical_json(p_claim->'value'),
    next_version,next_version-1,digest_value,p_event_id,p_claim->>'evidence',p_origin,p_model) RETURNING id INTO new_ledger_id;
  content_value:=entity||' = '||(p_claim->'value')::text;
  INSERT INTO public.semantic_embeddings(tenant_id,project_id,event_id,ledger_id,kind,state_at_capture,content,content_digest)
  VALUES(p_tenant_id,p_project_id,p_event_id,new_ledger_id,claim_kind,claim_state,content_value,digest_value)
  ON CONFLICT(tenant_id,project_id,content_digest) DO NOTHING RETURNING id INTO new_memory_id;
  IF new_memory_id IS NOT NULL THEN
    INSERT INTO public.ledger_outbox(tenant_id,project_id,event_id,memory_id,job_kind)
      VALUES(p_tenant_id,p_project_id,p_event_id,new_memory_id,'embedding');
  END IF;
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION validate_ledger_budget(p_project_id uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE accepted_count bigint; accepted_bytes bigint;
BEGIN
  IF (SELECT count(DISTINCT entity_key) FROM public.authoritative_constraints WHERE project_id=p_project_id)>128 THEN
    RAISE EXCEPTION 'ledger_key_budget_exceeded:128' USING ERRCODE='23514';
  END IF;
  SELECT count(*),COALESCE(sum(octet_length(entity_key)+octet_length(value::text)+octet_length(evidence)),0)
    INTO accepted_count,accepted_bytes FROM (
      SELECT DISTINCT ON(entity_key) entity_key,value,state,evidence FROM public.authoritative_constraints
      WHERE project_id=p_project_id AND state IN ('accepted','retracted') ORDER BY entity_key,version DESC
    ) authoritative WHERE state='accepted';
  IF accepted_count>32 OR accepted_bytes>16384 THEN
    RAISE EXCEPTION 'accepted_ledger_budget_exceeded:32_items_or_16384_bytes' USING ERRCODE='23514';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION execute_autonomous_sync(p_project_id uuid,p_query_text text,
  p_query_vector vector(1536),p_new_claims jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project_uuid uuid; tenant_uuid uuid; event_uuid uuid; memory_uuid uuid; event_row public.immutable_event_log%ROWTYPE;
  request_hash text; claims_array jsonb; source_value jsonb; fragment_value text; item jsonb; claim_count integer:=0;
  duplicate_count integer:=0; replayed boolean:=false; commit_result jsonb:='null'::jsonb;
  exact_context jsonb; constraints_context jsonb; versions jsonb; memories jsonb;
  pending_extract bigint; pending_embed bigint; dead_jobs bigint; ts_query tsquery; result_packet jsonb;
  ann_count integer:=0; used_semantic_fallback boolean:=false;
BEGIN
  project_uuid:=public.resolve_authorized_project(p_project_id); tenant_uuid:=public.authenticated_tenant_id();
  IF p_query_text IS NULL OR octet_length(p_query_text)>4000
     OR (p_query_vector IS NOT NULL AND (public.vector_dims(p_query_vector)<>1536 OR public.vector_norm(p_query_vector)=0))
  THEN RAISE EXCEPTION 'invalid_sync_query' USING ERRCODE='22023'; END IF;
  IF p_new_claims IS NOT NULL THEN
    IF NOT public.can_access_project(project_uuid,true) THEN RAISE EXCEPTION 'project_write_denied' USING ERRCODE='42501'; END IF;
    IF jsonb_typeof(p_new_claims) IS DISTINCT FROM 'object' OR octet_length(p_new_claims::text)>65536
      OR NOT(p_new_claims ?& ARRAY['idempotency_key','source','claims'])
      OR (p_new_claims-ARRAY['idempotency_key','source','fragment','claims'])<>'{}'::jsonb
      OR jsonb_typeof(p_new_claims->'idempotency_key') IS DISTINCT FROM 'string'
      OR (p_new_claims->>'idempotency_key') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    THEN RAISE EXCEPTION 'invalid_sync_envelope' USING ERRCODE='22023'; END IF;
    claims_array:=p_new_claims->'claims'; source_value:=p_new_claims->'source'; fragment_value:=p_new_claims->>'fragment';
    IF jsonb_typeof(claims_array) IS DISTINCT FROM 'array' OR jsonb_typeof(source_value) IS DISTINCT FROM 'object'
      OR octet_length(source_value::text)>2048 OR NOT(source_value ? 'client')
      OR jsonb_typeof(source_value->'client') IS DISTINCT FROM 'string'
      OR length(source_value->>'client') NOT BETWEEN 1 AND 80
      OR (source_value ? 'conversation_id' AND jsonb_typeof(source_value->'conversation_id') NOT IN ('string','null'))
      OR (source_value->>'conversation_id' IS NOT NULL AND length(source_value->>'conversation_id') NOT BETWEEN 1 AND 255)
      OR (source_value-ARRAY['client','conversation_id','message_id'])<>'{}'::jsonb
      OR (source_value ? 'message_id' AND jsonb_typeof(source_value->'message_id') NOT IN ('string','null'))
      OR (source_value->>'message_id' IS NOT NULL AND length(source_value->>'message_id') NOT BETWEEN 1 AND 255)
      OR (p_new_claims ? 'fragment' AND jsonb_typeof(p_new_claims->'fragment') NOT IN ('string','null'))
      OR (fragment_value IS NOT NULL AND octet_length(fragment_value) NOT BETWEEN 1 AND 16000)
    THEN RAISE EXCEPTION 'invalid_sync_source_or_claims' USING ERRCODE='22023'; END IF;
    IF jsonb_array_length(claims_array)>20 OR (jsonb_array_length(claims_array)=0 AND fragment_value IS NULL)
      OR (SELECT count(*) FROM jsonb_array_elements(claims_array))<>
         (SELECT count(DISTINCT lower(btrim(c->>'entity_key'))) FROM jsonb_array_elements(claims_array) c)
    THEN RAISE EXCEPTION 'invalid_claim_count_or_duplicate_entities' USING ERRCODE='22023'; END IF;
    request_hash:=public.ledger_digest(p_new_claims-'idempotency_key');
    PERFORM pg_advisory_xact_lock(hashtextextended(project_uuid::text,5));
    SELECT * INTO event_row FROM public.immutable_event_log
      WHERE tenant_id=tenant_uuid AND project_id=project_uuid AND idempotency_key=(p_new_claims->>'idempotency_key')::uuid;
    IF FOUND THEN
      IF event_row.content_digest<>request_hash THEN RAISE EXCEPTION 'idempotency_conflict' USING ERRCODE='23505'; END IF;
      event_uuid:=event_row.id; replayed:=true;
    ELSE
      PERFORM public.charge_usage('commits');
      INSERT INTO public.immutable_event_log(tenant_id,project_id,actor_key_id,idempotency_key,content_digest,source,fragment,payload)
      VALUES(tenant_uuid,project_uuid,NULLIF(current_setting('app.api_key_id',true),'')::uuid,
        (p_new_claims->>'idempotency_key')::uuid,request_hash,source_value,fragment_value,p_new_claims) RETURNING id INTO event_uuid;
      FOR item IN SELECT * FROM jsonb_array_elements(claims_array) LOOP
        IF public.append_ledger_claim(tenant_uuid,project_uuid,event_uuid,item,'host',NULL) THEN claim_count:=claim_count+1;
        ELSE duplicate_count:=duplicate_count+1; END IF;
      END LOOP;
      IF fragment_value IS NOT NULL THEN
        INSERT INTO public.ledger_outbox(tenant_id,project_id,event_id,job_kind)
          VALUES(tenant_uuid,project_uuid,event_uuid,'extraction');
        INSERT INTO public.semantic_embeddings(tenant_id,project_id,event_id,kind,state_at_capture,content,content_digest)
          VALUES(tenant_uuid,project_uuid,event_uuid,'fragment','tentative',fragment_value,
            public.ledger_digest(jsonb_build_object('kind','fragment','content',fragment_value)))
          ON CONFLICT(tenant_id,project_id,content_digest) DO NOTHING RETURNING id INTO memory_uuid;
        IF memory_uuid IS NOT NULL THEN
          INSERT INTO public.ledger_outbox(tenant_id,project_id,event_id,memory_id,job_kind)
            VALUES(tenant_uuid,project_uuid,event_uuid,memory_uuid,'embedding');
        END IF;
      END IF;
    END IF;
    commit_result:=jsonb_build_object('event_id',event_uuid,'replayed',replayed,
      'claims_inserted',claim_count,'claims_deduplicated',duplicate_count,'durable',true);
  END IF;
  PERFORM public.validate_ledger_budget(project_uuid);
  PERFORM public.charge_usage('recalls');
  PERFORM set_config('hnsw.ef_search','100',true);
  PERFORM set_config('hnsw.iterative_scan','strict_order',true);
  PERFORM set_config('hnsw.max_scan_tuples','20000',true);
  PERFORM set_config('hnsw.scan_mem_multiplier','2',true);
  SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'entity_key',entity_key,'kind',kind,'state',state,'value',value,
      'version',version,'event_id',event_id,'evidence',evidence,'created_at',created_at) ORDER BY entity_key),'[]'::jsonb)
    INTO exact_context FROM (
      SELECT DISTINCT ON(entity_key) * FROM public.authoritative_constraints
      WHERE project_id=project_uuid AND state IN ('accepted','retracted') ORDER BY entity_key,version DESC
    ) latest WHERE state='accepted';
  SELECT COALESCE(jsonb_agg(c),'[]'::jsonb) INTO constraints_context
    FROM jsonb_array_elements(exact_context) c WHERE c->>'kind'='constraint';
  SELECT COALESCE(jsonb_object_agg(entity_key,version),'{}'::jsonb) INTO versions FROM (
    SELECT entity_key,max(version) AS version FROM public.authoritative_constraints WHERE project_id=project_uuid GROUP BY entity_key
  ) heads;
  ts_query:=websearch_to_tsquery('simple'::regconfig,p_query_text);
  -- Reciprocal-rank fusion retains exact-term candidates when an embedding is
  -- unavailable. The distance ORDER BY is index-compatible; HNSW is approximate.
  WITH ann_candidates AS MATERIALIZED (
    SELECT id,embedding OPERATOR(public.<=>) p_query_vector AS distance FROM public.semantic_embeddings
    WHERE project_id=project_uuid AND embedding IS NOT NULL AND p_query_vector IS NOT NULL
    ORDER BY embedding OPERATOR(public.<=>) p_query_vector LIMIT 40
  ), authorized_vectors AS MATERIALIZED (
    -- Materialize the authorized project BEFORE ordering, preventing another ANN
    -- plan from repeating the underfilled scan. This exact fallback scans project
    -- vectors only, and remains bounded by the runtime statement timeout.
    SELECT id,embedding FROM public.semantic_embeddings
    WHERE project_id=project_uuid AND embedding IS NOT NULL AND p_query_vector IS NOT NULL
      AND (SELECT count(*) FROM ann_candidates)<8
  ), exact_fallback AS MATERIALIZED (
    SELECT id,embedding OPERATOR(public.<=>) p_query_vector AS distance FROM authorized_vectors
    ORDER BY embedding OPERATOR(public.<=>) p_query_vector,id LIMIT 40
  ), semantic_candidates AS (
    SELECT id,distance FROM ann_candidates WHERE (SELECT count(*) FROM ann_candidates)>=8
    UNION ALL
    SELECT id,distance FROM exact_fallback WHERE (SELECT count(*) FROM ann_candidates)<8
  ), semantic_rank AS (
    SELECT id,distance,row_number() OVER(ORDER BY distance,id) AS rank FROM semantic_candidates
  ), lexical_candidates AS MATERIALIZED (
    SELECT id,ts_rank_cd(search_document,ts_query) AS score FROM public.semantic_embeddings
    WHERE project_id=project_uuid AND search_document@@ts_query
    ORDER BY ts_rank_cd(search_document,ts_query) DESC,id LIMIT 40
  ), lexical_rank AS (
    SELECT id,row_number() OVER(ORDER BY score DESC,id) AS rank FROM lexical_candidates
  ), fused AS (
    SELECT COALESCE(s.id,l.id) AS id,s.distance,
      COALESCE(1.0/(60+s.rank),0)+COALESCE(1.0/(60+l.rank),0) AS score
    FROM semantic_rank s FULL JOIN lexical_rank l ON s.id=l.id
    ORDER BY score DESC,COALESCE(s.id,l.id) LIMIT 8
  ) SELECT COALESCE(jsonb_agg(jsonb_build_object('memory_id',v.id,'event_id',v.event_id,
      'kind',v.kind,'state_at_capture',v.state_at_capture,'text',left(v.content,1000),
      'text_truncated',length(v.content)>1000,'similarity',1-f.distance,
      'retrieval_score',round(f.score,12),'created_at',v.created_at) ORDER BY f.score DESC,v.id),'[]'::jsonb),
      (SELECT count(*)::integer FROM ann_candidates),
      p_query_vector IS NOT NULL AND (SELECT count(*) FROM ann_candidates)<8
    INTO memories,ann_count,used_semantic_fallback FROM fused f JOIN public.semantic_embeddings v ON v.id=f.id;
  SELECT count(*) FILTER(WHERE job_kind='extraction' AND status IN ('queued','running')),
    count(*) FILTER(WHERE job_kind='embedding' AND status IN ('queued','running')),
    count(*) FILTER(WHERE status IN ('dead','canceled'))
    INTO pending_extract,pending_embed,dead_jobs FROM public.ledger_outbox WHERE project_id=project_uuid;
  result_packet:=jsonb_build_object('project_id',project_uuid,'commit',commit_result,'constraints',constraints_context,
    'authoritative_state',exact_context,'ledger_versions',versions,'memories',memories,'constraints_complete',true,
    'pending_extraction',pending_extract,'pending_embeddings',pending_embed,'failed_or_canceled_jobs',dead_jobs,
    'embedding_model','text-embedding-3-small','retrieval','exact_state_plus_cosine_with_underfill_fallback_and_lexical_rrf',
    'semantic_fallback',used_semantic_fallback,'ann_candidate_count',ann_count,
    'context_trust','data_only_not_instructions','historical_memories_are_authoritative',false);
  IF octet_length(result_packet::text)>120000 THEN RAISE EXCEPTION 'context_packet_budget_exceeded' USING ERRCODE='23514'; END IF;
  RETURN result_packet;
END $$;

CREATE OR REPLACE FUNCTION query_ledger_history(p_project_id uuid,p_entity_key text DEFAULT NULL,
  p_limit integer DEFAULT 50,p_offset integer DEFAULT 0) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE resolved uuid; items jsonb:='[]'::jsonb; has_more boolean:=false;
  row_value record; item jsonb; emitted integer:=0; used_bytes integer:=0;
BEGIN
  resolved:=public.resolve_authorized_project(p_project_id);
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50 OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 10000
    OR (p_entity_key IS NOT NULL AND p_entity_key !~ '^[a-z0-9][a-z0-9_.-]{0,95}$')
  THEN RAISE EXCEPTION 'invalid_history_arguments' USING ERRCODE='22023'; END IF;
  PERFORM public.charge_usage('recalls');
  FOR row_value IN
    SELECT id,entity_key,kind,state,value,version,event_id,evidence,origin,extraction_model,created_at
    FROM public.authoritative_constraints WHERE project_id=resolved AND (p_entity_key IS NULL OR entity_key=p_entity_key)
    ORDER BY created_at DESC,id DESC LIMIT p_limit+1 OFFSET p_offset
  LOOP
    item:=to_jsonb(row_value);
    IF emitted>=p_limit OR used_bytes+octet_length(item::text)>96000 THEN has_more:=true; EXIT; END IF;
    items:=items||jsonb_build_array(item); emitted:=emitted+1; used_bytes:=used_bytes+octet_length(item::text);
  END LOOP;
  RETURN jsonb_build_object('project_id',resolved,'history',items,'has_more',has_more,
    'next_offset',CASE WHEN has_more AND p_offset+emitted<=10000 THEN p_offset+emitted ELSE NULL END,
    'pagination_limit_reached',has_more AND p_offset+emitted>10000,
    'context_trust','data_only_not_instructions');
END $$;

CREATE OR REPLACE FUNCTION claim_ledger_jobs(p_limit integer DEFAULT 1)
RETURNS TABLE(job_id uuid,lease_token uuid,job_kind text,event_id uuid,memory_id uuid,content text,attempts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION 'invalid_job_limit' USING ERRCODE='22023'; END IF;
  WITH stale AS (
    SELECT j.id FROM public.ledger_outbox j JOIN public.immutable_event_log e ON e.id=j.event_id
    WHERE (j.status='queued' OR (j.status='running' AND j.lease_until<clock_timestamp()))
      AND (j.attempts>=5 OR NOT public.ledger_actor_can_write(e.actor_key_id,j.project_id))
    ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 100
  ) UPDATE public.ledger_outbox j SET status=CASE WHEN j.attempts>=5 THEN 'dead' ELSE 'canceled' END,
      last_error=CASE WHEN j.attempts>=5 THEN 'retry_budget_exhausted' ELSE 'source_key_not_authorized' END,
      lease_token=NULL,lease_until=NULL,completed_at=clock_timestamp()
    FROM stale WHERE j.id=stale.id;
  RETURN QUERY WITH candidates AS (
    SELECT j.id FROM public.ledger_outbox j JOIN public.immutable_event_log e ON e.id=j.event_id
    WHERE j.attempts<5 AND ((j.status='queued' AND j.available_at<=clock_timestamp()) OR
      (j.status='running' AND j.lease_until<clock_timestamp()))
      AND public.ledger_actor_can_write(e.actor_key_id,j.project_id)
    ORDER BY j.available_at,j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT p_limit
  ), claimed AS (
    UPDATE public.ledger_outbox j SET status='running',attempts=j.attempts+1,
      lease_until=clock_timestamp()+interval '120 seconds',lease_token=gen_random_uuid()
    FROM candidates c WHERE j.id=c.id RETURNING j.*
  ) SELECT c.id,c.lease_token,c.job_kind,c.event_id,c.memory_id,
      CASE WHEN c.job_kind='extraction' THEN e.fragment ELSE v.content END,c.attempts
    FROM claimed c JOIN public.immutable_event_log e ON e.id=c.event_id
    LEFT JOIN public.semantic_embeddings v ON v.id=c.memory_id;
END $$;

CREATE OR REPLACE FUNCTION finish_ledger_embedding_job(p_job_id uuid,p_lease_token uuid,p_embedding vector(1536),p_model text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.ledger_outbox%ROWTYPE; actor uuid;
BEGIN
  IF p_embedding IS NULL OR public.vector_dims(p_embedding)<>1536 OR public.vector_norm(p_embedding)=0
    OR p_model IS DISTINCT FROM 'text-embedding-3-small' THEN RAISE EXCEPTION 'invalid_embedding' USING ERRCODE='22023'; END IF;
  SELECT * INTO job FROM public.ledger_outbox WHERE id=p_job_id AND lease_token=p_lease_token
    AND job_kind='embedding' AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT actor_key_id INTO actor FROM public.immutable_event_log WHERE id=job.event_id;
  IF NOT public.ledger_actor_can_write(actor,job.project_id) THEN
    UPDATE public.ledger_outbox SET status='canceled',last_error='source_key_not_authorized',
      lease_until=NULL,lease_token=NULL,completed_at=clock_timestamp() WHERE id=job.id;
    RETURN false;
  END IF;
  UPDATE public.semantic_embeddings SET embedding=p_embedding,embedding_model=p_model
    WHERE id=job.memory_id AND tenant_id=job.tenant_id AND project_id=job.project_id AND embedding IS NULL;
  UPDATE public.ledger_outbox SET status='done',lease_until=NULL,lease_token=NULL,last_error=NULL,
    result=jsonb_build_object('model',p_model),completed_at=clock_timestamp() WHERE id=job.id;
  RETURN true;
END $$;

CREATE OR REPLACE FUNCTION finish_ledger_extraction_job(p_job_id uuid,p_lease_token uuid,p_claims jsonb,p_model text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.ledger_outbox%ROWTYPE; actor uuid; item jsonb; live_version integer; inserted integer:=0; normalized_entity text;
BEGIN
  IF jsonb_typeof(p_claims) IS DISTINCT FROM 'array' OR octet_length(p_claims::text)>49152
    OR p_model IS NULL OR length(p_model) NOT BETWEEN 1 AND 120
  THEN RAISE EXCEPTION 'invalid_extraction' USING ERRCODE='22023'; END IF;
  IF jsonb_array_length(p_claims)>20 OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_claims) c WHERE c->>'state' IS DISTINCT FROM 'tentative')
    OR (SELECT count(*) FROM jsonb_array_elements(p_claims))<>
       (SELECT count(DISTINCT lower(btrim(c->>'entity_key'))) FROM jsonb_array_elements(p_claims) c)
  THEN RAISE EXCEPTION 'extraction_must_be_distinct_tentative_claims' USING ERRCODE='22023'; END IF;
  SELECT * INTO job FROM public.ledger_outbox WHERE id=p_job_id AND lease_token=p_lease_token
    AND job_kind='extraction' AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT actor_key_id INTO actor FROM public.immutable_event_log WHERE id=job.event_id;
  IF NOT public.ledger_actor_can_write(actor,job.project_id) THEN
    UPDATE public.ledger_outbox SET status='canceled',last_error='source_key_not_authorized',
      lease_until=NULL,lease_token=NULL,completed_at=clock_timestamp() WHERE id=job.id;
    RETURN false;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(job.project_id::text,5));
  -- Extraction never asserts acceptance. Reconcile proposals against the current
  -- sequence under the same lock as foreground writers; accepted heads are untouched.
  FOR item IN SELECT * FROM jsonb_array_elements(p_claims) LOOP
    normalized_entity:=lower(btrim(item->>'entity_key'));
    SELECT COALESCE(max(version),0) INTO live_version FROM public.authoritative_constraints
      WHERE tenant_id=job.tenant_id AND project_id=job.project_id AND entity_key=normalized_entity;
    item:=jsonb_set(item,'{expected_version}',to_jsonb(live_version));
    IF public.append_ledger_claim(job.tenant_id,job.project_id,job.event_id,item,'extractor',p_model) THEN inserted:=inserted+1; END IF;
  END LOOP;
  PERFORM public.validate_ledger_budget(job.project_id);
  UPDATE public.ledger_outbox SET status='done',lease_until=NULL,lease_token=NULL,last_error=NULL,
    result=jsonb_build_object('model',p_model,'claims_inserted',inserted),completed_at=clock_timestamp() WHERE id=job.id;
  RETURN true;
END $$;

SET LOCAL ROLE hivemind_owner;
-- Accepted/retracted source rows are never rewritten. The effective lifecycle is
-- derived: only the newest non-tentative revision may be active or retracted;
-- older non-tentative rows are superseded. Tentative discussion does not displace
-- the accepted head and remains explicitly tentative until a later revision.
CREATE VIEW authoritative_constraint_states WITH(security_invoker=true,security_barrier=true) AS
SELECT l.*,
  CASE
    WHEN l.state='tentative' THEN 'tentative'
    WHEN EXISTS(SELECT 1 FROM authoritative_constraints newer
      WHERE newer.tenant_id=l.tenant_id AND newer.project_id=l.project_id
        AND newer.entity_key=l.entity_key AND newer.version>l.version
        AND newer.state IN ('accepted','retracted')) THEN 'superseded'
    WHEN l.state='accepted' THEN 'active'
    ELSE 'retracted'
  END AS lifecycle_state
FROM authoritative_constraints l;
GRANT SELECT ON authoritative_constraint_states TO hivemind_app,hivemind_ledger_writer;
COMMENT ON TABLE authoritative_constraints IS 'Immutable revisions. state is declared tentative/accepted/retracted; authoritative_constraint_states derives active/superseded/retracted without mutating history.';
COMMENT ON TABLE semantic_embeddings IS 'Canonical pgvector(1536) store; inherited vector_knowledge_cosine HNSW index uses cosine m=16 ef_construction=64. Tenant/project filtering is logical RLS, not cryptographic encryption.';

CREATE TABLE project_audit_chain (
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  sequence bigint NOT NULL CHECK(sequence>0),
  entry_type text NOT NULL CHECK(entry_type IN ('event','ledger_revision')),
  source_id uuid NOT NULL,
  source_content_digest text NOT NULL CHECK(source_content_digest ~ '^[0-9a-f]{64}$'),
  previous_hash text NOT NULL CHECK(previous_hash ~ '^[0-9a-f]{64}$'),
  chain_hash text NOT NULL CHECK(chain_hash ~ '^[0-9a-f]{64}$'),
  commitment jsonb NOT NULL CHECK(jsonb_typeof(commitment)='object' AND octet_length(commitment::text)<=4096),
  recorded_at timestamptz NOT NULL,
  historical_backfill boolean NOT NULL DEFAULT false,
  PRIMARY KEY(tenant_id,project_id,sequence),
  UNIQUE(tenant_id,project_id,entry_type,source_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
);
ALTER TABLE project_audit_chain ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_audit_chain FORCE ROW LEVEL SECURITY;
CREATE POLICY scoped_audit_read ON project_audit_chain FOR SELECT TO hivemind_app,hivemind_ledger_writer
  USING(tenant_id=public.authenticated_tenant_id() AND public.can_access_project(project_id,false));
-- The NOLOGIN audit role is reachable only through ungranted append helpers and
-- triggers on tables whose writes are already restricted. It is not a runtime role.
CREATE POLICY internal_audit_writer ON project_audit_chain TO hivemind_audit_writer USING(true) WITH CHECK(true);
GRANT SELECT ON project_audit_chain TO hivemind_app,hivemind_ledger_writer,hivemind_audit_writer;
GRANT INSERT ON project_audit_chain TO hivemind_audit_writer;
CREATE TRIGGER project_audit_chain_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON project_audit_chain
  FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation();

CREATE FUNCTION append_project_audit(p_entry_type text,p_row jsonb,p_historical_backfill boolean DEFAULT false)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp SET timezone='UTC' AS $$
DECLARE tenant_uuid uuid:=(p_row->>'tenant_id')::uuid; project_uuid uuid:=(p_row->>'project_id')::uuid;
  source_uuid uuid:=(p_row->>'id')::uuid; prior_hash text; next_sequence bigint;
  commitment_value jsonb; recorded timestamptz; existing_digest text;
BEGIN
  IF p_entry_type NOT IN ('event','ledger_revision') OR p_row IS NULL
     OR NOT(p_row ?& ARRAY['tenant_id','project_id','id','content_digest','created_at'])
  THEN RAISE EXCEPTION 'invalid_audit_source' USING ERRCODE='22023'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(project_uuid::text,5));
  SELECT commitment->>'row_digest' INTO existing_digest FROM public.project_audit_chain
    WHERE tenant_id=tenant_uuid AND project_id=project_uuid AND entry_type=p_entry_type AND source_id=source_uuid;
  IF FOUND THEN
    IF existing_digest<>public.ledger_digest(p_row) THEN
      RAISE EXCEPTION 'audit_source_conflict' USING ERRCODE='23505';
    END IF;
    RETURN;
  END IF;
  SELECT sequence+1,chain_hash INTO next_sequence,prior_hash FROM public.project_audit_chain
    WHERE tenant_id=tenant_uuid AND project_id=project_uuid ORDER BY sequence DESC LIMIT 1;
  next_sequence:=COALESCE(next_sequence,1); prior_hash:=COALESCE(prior_hash,repeat('0',64));
  recorded:=clock_timestamp();
  commitment_value:=jsonb_build_object(
    'domain','hivemind.project-audit.v1','tenant_id',tenant_uuid,'project_id',project_uuid,
    'sequence',next_sequence,'entry_type',p_entry_type,'source_id',source_uuid,
    'source_content_digest',p_row->>'content_digest','row_digest',public.ledger_digest(p_row),
    'source_created_at',p_row->>'created_at','recorded_at',recorded,
    'previous_hash',prior_hash,'historical_backfill',p_historical_backfill);
  INSERT INTO public.project_audit_chain(tenant_id,project_id,sequence,entry_type,source_id,
    source_content_digest,previous_hash,chain_hash,commitment,recorded_at,historical_backfill)
  VALUES(tenant_uuid,project_uuid,next_sequence,p_entry_type,source_uuid,p_row->>'content_digest',
    prior_hash,public.ledger_digest(commitment_value),commitment_value,recorded,p_historical_backfill);
END $$;
CREATE FUNCTION capture_project_audit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp SET timezone='UTC' AS $$
BEGIN
  PERFORM public.append_project_audit(
    CASE TG_TABLE_NAME WHEN 'immutable_event_log' THEN 'event' WHEN 'authoritative_constraints' THEN 'ledger_revision' END,
    to_jsonb(NEW),false);
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_event_log_audit AFTER INSERT ON immutable_event_log
  FOR EACH ROW EXECUTE FUNCTION capture_project_audit();
CREATE TRIGGER authoritative_constraints_audit AFTER INSERT ON authoritative_constraints
  FOR EACH ROW EXECUTE FUNCTION capture_project_audit();
RESET ROLE;
ALTER FUNCTION append_project_audit(text,jsonb,boolean) OWNER TO hivemind_audit_writer;
ALTER FUNCTION capture_project_audit() OWNER TO hivemind_audit_writer;
REVOKE ALL ON FUNCTION append_project_audit(text,jsonb,boolean),capture_project_audit() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ledger_canonical_json(jsonb),ledger_digest(jsonb) TO hivemind_audit_writer;

-- Upgrade existing 005 rows under the administrator's migration transaction.
-- ALTER TABLE above holds ACCESS EXCLUSIVE until COMMIT, so inserts cannot race
-- the backfill. Timestamp/UUID order is reproducible, but is not claimed to be the
-- original transaction order. The historical_backfill bit exposes this boundary.
SET LOCAL timezone='UTC';
DO $$ DECLARE existing_row record; BEGIN
  FOR existing_row IN
    SELECT entry_type,payload FROM (
      SELECT 'event'::text AS entry_type,to_jsonb(e) AS payload,e.created_at,e.id,0 AS kind_order FROM public.immutable_event_log e
      UNION ALL
      SELECT 'ledger_revision'::text,to_jsonb(l),l.created_at,l.id,1 FROM public.authoritative_constraints l
    ) existing ORDER BY created_at,kind_order,id
  LOOP
    PERFORM public.append_project_audit(existing_row.entry_type,existing_row.payload,true);
  END LOOP;
END $$;

SET LOCAL ROLE hivemind_owner;
CREATE FUNCTION execute_atomic_sync(p_project_id uuid,p_query_text text,p_query_vector vector(1536),p_new_claims jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp SET timezone='UTC' AS $$
DECLARE project_uuid uuid; response_packet jsonb; checkpoint jsonb; receipt jsonb; canonical_receipt text;
  head_sequence bigint; head_hash text;
BEGIN
  project_uuid:=public.resolve_authorized_project(p_project_id);
  -- All foreground revisions, asynchronous proposals, audit appends and audited
  -- reads use the same project transaction lock. A successful receipt binds one
  -- coherent project snapshot. Database lock/statement timeouts remain authoritative.
  PERFORM pg_advisory_xact_lock(hashtextextended(project_uuid::text,5));
  response_packet:=public.execute_autonomous_sync(project_uuid,p_query_text,p_query_vector,p_new_claims);
  SELECT sequence,chain_hash INTO head_sequence,head_hash FROM public.project_audit_chain
    WHERE project_id=project_uuid ORDER BY sequence DESC LIMIT 1;
  head_sequence:=COALESCE(head_sequence,0); head_hash:=COALESCE(head_hash,repeat('0',64));
  checkpoint:=jsonb_build_object('sequence',head_sequence,'chain_hash',head_hash);
  receipt:=jsonb_build_object('domain','hivemind.sync-receipt.v1','invocation_id',gen_random_uuid(),
    'observed_at',clock_timestamp(),'project_id',project_uuid,
    'actor_key_id',NULLIF(current_setting('app.api_key_id',true),'')::uuid,
    'event_id',response_packet#>>'{commit,event_id}','chain_sequence',head_sequence,'chain_hash',head_hash,
    'state_digest',public.ledger_digest(response_packet->'authoritative_state'),
    'response_digest',public.ledger_digest(response_packet),
    'request_digest',public.ledger_digest(jsonb_build_object('project_id',project_uuid,'query_text',p_query_text,
      'query_vector',CASE WHEN p_query_vector IS NULL THEN NULL ELSE p_query_vector::text END,'new_claims',p_new_claims)));
  canonical_receipt:=public.ledger_canonical_json(receipt)::text;
  RETURN response_packet||jsonb_build_object('audit_checkpoint',checkpoint,'invocation_receipt',
    receipt||jsonb_build_object('canonical_receipt',canonical_receipt,'receipt_hash',public.ledger_digest(receipt),
      'attestation','unsigned_commitment_retain_or_sign_externally','durably_stored',false));
END $$;

CREATE FUNCTION query_project_audit(p_project_id uuid,p_after_sequence bigint DEFAULT 0,p_limit integer DEFAULT 50)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp SET timezone='UTC' AS $$
DECLARE project_uuid uuid; row_value record; source_row jsonb; source_preimage jsonb; item jsonb;
  items jsonb:='[]'::jsonb; used_bytes integer:=0; emitted integer:=0; next_sequence bigint;
  head_sequence bigint; head_hash text; has_more boolean:=false;
BEGIN
  project_uuid:=public.resolve_authorized_project(p_project_id);
  IF p_after_sequence IS NULL OR p_after_sequence<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 50 THEN
    RAISE EXCEPTION 'invalid_audit_page' USING ERRCODE='22023';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(project_uuid::text,5));
  PERFORM public.charge_usage('recalls');
  SELECT sequence,chain_hash INTO head_sequence,head_hash FROM public.project_audit_chain
    WHERE project_id=project_uuid ORDER BY sequence DESC LIMIT 1;
  next_sequence:=p_after_sequence;
  FOR row_value IN SELECT * FROM public.project_audit_chain
    WHERE project_id=project_uuid AND sequence>p_after_sequence ORDER BY sequence LIMIT p_limit+1
  LOOP
    IF emitted>=p_limit THEN has_more:=true; EXIT; END IF;
    IF row_value.entry_type='event' THEN
      SELECT to_jsonb(e),e.payload-'idempotency_key' INTO source_row,source_preimage
        FROM public.immutable_event_log e WHERE e.project_id=project_uuid AND e.id=row_value.source_id;
    ELSE
      SELECT to_jsonb(l),jsonb_build_object('entity_key',l.entity_key,'kind',l.kind,'state',l.state,'value',l.value)
        INTO source_row,source_preimage FROM public.authoritative_constraints l
        WHERE l.project_id=project_uuid AND l.id=row_value.source_id;
    END IF;
    IF source_row IS NULL THEN RAISE EXCEPTION 'audit_source_missing' USING ERRCODE='55000'; END IF;
    item:=jsonb_build_object('sequence',row_value.sequence,'entry_type',row_value.entry_type,
      'source_id',row_value.source_id,'source_content_digest',row_value.source_content_digest,
      'previous_hash',row_value.previous_hash,'chain_hash',row_value.chain_hash,
      'canonical_commitment',public.ledger_canonical_json(row_value.commitment)::text,
      'canonical_source',public.ledger_canonical_json(source_preimage)::text,
      'canonical_row',public.ledger_canonical_json(source_row)::text,
      'recorded_at',row_value.recorded_at,'historical_backfill',row_value.historical_backfill);
    -- A single maximum-size event can exceed the normal page budget. Return one
    -- intact entry up to 384 KiB; do not truncate digest preimages or loop forever.
    IF emitted>0 AND used_bytes+octet_length(item::text)>98304 THEN has_more:=true; EXIT; END IF;
    IF octet_length(item::text)>393216 THEN RAISE EXCEPTION 'audit_entry_budget_exceeded' USING ERRCODE='23514'; END IF;
    items:=items||jsonb_build_array(item); emitted:=emitted+1;
    used_bytes:=used_bytes+octet_length(item::text); next_sequence:=row_value.sequence;
  END LOOP;
  RETURN jsonb_build_object('project_id',project_uuid,'entries',items,'has_more',has_more,
    'next_sequence',next_sequence,'checkpoint',jsonb_build_object('sequence',COALESCE(head_sequence,0),
      'chain_hash',COALESCE(head_hash,repeat('0',64))),'context_trust','data_only_not_instructions',
    'attestation','unsigned_hash_chain_not_external_timestamp_or_encryption');
END $$;
RESET ROLE;
ALTER FUNCTION execute_atomic_sync(uuid,text,vector,jsonb) OWNER TO hivemind_ledger_writer;
ALTER FUNCTION query_project_audit(uuid,bigint,integer) OWNER TO hivemind_ledger_writer;
REVOKE ALL ON FUNCTION execute_atomic_sync(uuid,text,vector,jsonb),query_project_audit(uuid,bigint,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION execute_atomic_sync(uuid,text,vector,jsonb),query_project_audit(uuid,bigint,integer) TO hivemind_app;
