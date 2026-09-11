-- Apply after 000-004 using the checksum-tracking migration runner in one transaction.
-- PostgreSQL 17 + pgvector >= 0.8.0. This is additive: legacy ledgers stay intact.
-- Runtime roles never own tables, inherit helper roles, or receive BYPASSRLS.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hivemind_ledger_writer') THEN
    CREATE ROLE hivemind_ledger_writer NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO hivemind_ledger_writer;
SET ROLE hivemind_owner;

-- JSONB normalizes object key order and JSON whitespace. Removing numeric scale
-- also makes 1, 1.0 and 1e0 identical. String whitespace/case is meaningful and
-- deliberately preserved; this is syntax deduplication, not semantic equivalence.
CREATE FUNCTION ledger_canonical_json(p_value jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result jsonb;
BEGIN
  CASE jsonb_typeof(p_value)
    WHEN 'object' THEN
      SELECT COALESCE(jsonb_object_agg(k,public.ledger_canonical_json(v)),'{}'::jsonb)
        INTO result FROM jsonb_each(p_value) AS pairs(k,v);
    WHEN 'array' THEN
      SELECT COALESCE(jsonb_agg(public.ledger_canonical_json(v) ORDER BY n),'[]'::jsonb)
        INTO result FROM jsonb_array_elements(p_value) WITH ORDINALITY AS items(v,n);
    WHEN 'number' THEN result:=to_jsonb(trim_scale((p_value#>>'{}')::numeric));
    ELSE result:=p_value;
  END CASE;
  RETURN result;
END $$;
CREATE FUNCTION ledger_digest(p_value jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path=pg_catalog,public,pg_temp AS $$
  SELECT encode(sha256(convert_to(public.ledger_canonical_json(p_value)::text,'UTF8')),'hex')
$$;

CREATE TABLE memory_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  actor_key_id uuid NOT NULL,
  idempotency_key uuid NOT NULL,
  content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
  source jsonb NOT NULL CHECK(jsonb_typeof(source)='object' AND octet_length(source::text)<=2048),
  fragment text CHECK(fragment IS NULL OR octet_length(fragment) BETWEEN 1 AND 16000),
  payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND octet_length(payload::text)<=65536),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(tenant_id,project_id,id),
  UNIQUE(tenant_id,project_id,idempotency_key),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
  FOREIGN KEY(tenant_id,actor_key_id) REFERENCES api_keys(tenant_id,id)
);
CREATE INDEX memory_events_recent ON memory_events(tenant_id,project_id,created_at DESC,id DESC);
CREATE INDEX memory_events_digest ON memory_events(tenant_id,project_id,content_digest);
CREATE TABLE ledger_constraints (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  entity_key text NOT NULL CHECK(entity_key ~ '^[a-z0-9][a-z0-9_.-]{0,95}$'),
  kind text NOT NULL CHECK(kind IN ('fact','constraint','decision','config','task')),
  state text NOT NULL CHECK(state IN ('tentative','accepted','retracted')),
  value jsonb NOT NULL CHECK(octet_length(value::text)<=2048),
  version integer NOT NULL CHECK(version>0),
  expected_version integer NOT NULL CHECK(expected_version>=0 AND version=expected_version+1),
  content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
  event_id uuid NOT NULL,
  evidence text NOT NULL CHECK(octet_length(evidence) BETWEEN 1 AND 2000),
  origin text NOT NULL CHECK(origin IN ('host','extractor')),
  extraction_model text CHECK(extraction_model IS NULL OR length(extraction_model) BETWEEN 1 AND 120),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(tenant_id,project_id,id),
  UNIQUE(tenant_id,project_id,entity_key,version),
  FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES memory_events(tenant_id,project_id,id),
  CHECK(origin<>'extractor' OR (state='tentative' AND extraction_model IS NOT NULL))
);
CREATE INDEX ledger_constraints_heads ON ledger_constraints(tenant_id,project_id,entity_key,version DESC);
CREATE INDEX ledger_constraints_authoritative ON ledger_constraints(tenant_id,project_id,entity_key,version DESC)
  WHERE state IN ('accepted','retracted');
CREATE INDEX ledger_constraints_digest ON ledger_constraints(tenant_id,project_id,entity_key,content_digest);
CREATE TABLE vector_knowledge (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  event_id uuid NOT NULL,
  ledger_id uuid,
  kind text NOT NULL CHECK(kind IN ('fragment','fact','constraint','decision','config','task')),
  state_at_capture text NOT NULL CHECK(state_at_capture IN ('tentative','accepted','retracted')),
  content text NOT NULL CHECK(octet_length(content) BETWEEN 1 AND 16000),
  content_digest text NOT NULL CHECK(content_digest ~ '^[0-9a-f]{64}$'),
  embedding vector(1536),
  embedding_model text NOT NULL DEFAULT 'text-embedding-3-small' CHECK(embedding_model='text-embedding-3-small'),
  search_document tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig,content)) STORED,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(tenant_id,project_id,id),
  UNIQUE(tenant_id,project_id,content_digest),
  FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES memory_events(tenant_id,project_id,id),
  FOREIGN KEY(tenant_id,project_id,ledger_id) REFERENCES ledger_constraints(tenant_id,project_id,id),
  CHECK(embedding IS NULL OR vector_norm(embedding)>0)
);
CREATE INDEX vector_knowledge_recent ON vector_knowledge(tenant_id,project_id,created_at DESC);
CREATE INDEX vector_knowledge_cosine ON vector_knowledge USING hnsw(embedding vector_cosine_ops) WITH(m=16,ef_construction=64);
CREATE INDEX vector_knowledge_lexical ON vector_knowledge USING gin(search_document);
CREATE TABLE ledger_outbox (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  event_id uuid NOT NULL,
  memory_id uuid,
  job_kind text NOT NULL CHECK(job_kind IN ('extraction','embedding')),
  status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','dead','canceled')),
  attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 5),
  available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_until timestamptz,
  lease_token uuid,
  last_error text CHECK(last_error IS NULL OR last_error ~ '^[a-zA-Z0-9_.:-]{1,120}$'),
  result jsonb CHECK(result IS NULL OR (jsonb_typeof(result)='object' AND octet_length(result::text)<=4096)),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES memory_events(tenant_id,project_id,id),
  FOREIGN KEY(tenant_id,project_id,memory_id) REFERENCES vector_knowledge(tenant_id,project_id,id),
  CHECK((job_kind='extraction' AND memory_id IS NULL) OR (job_kind='embedding' AND memory_id IS NOT NULL)),
  CHECK((status='running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
     OR (status<>'running' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE UNIQUE INDEX ledger_outbox_extract_once ON ledger_outbox(event_id) WHERE job_kind='extraction';
CREATE UNIQUE INDEX ledger_outbox_embed_once ON ledger_outbox(memory_id) WHERE job_kind='embedding';
CREATE INDEX ledger_outbox_ready ON ledger_outbox(available_at,created_at) WHERE status='queued';
CREATE INDEX ledger_outbox_expired ON ledger_outbox(lease_until) WHERE status='running';
CREATE INDEX ledger_outbox_project_status ON ledger_outbox(tenant_id,project_id,status,job_kind);

CREATE FUNCTION reject_ledger_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  RAISE EXCEPTION 'immutable_history:%',TG_TABLE_NAME USING ERRCODE='55000';
END $$;
CREATE TRIGGER memory_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON memory_events
  FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation();
CREATE TRIGGER ledger_constraints_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON ledger_constraints
  FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation();
CREATE FUNCTION guard_vector_content() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF (to_jsonb(NEW)-ARRAY['embedding','embedding_model','search_document']) IS DISTINCT FROM
     (to_jsonb(OLD)-ARRAY['embedding','embedding_model','search_document'])
    OR OLD.embedding IS NOT NULL THEN
    RAISE EXCEPTION 'immutable_vector_content' USING ERRCODE='55000';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER vector_content_immutable BEFORE UPDATE ON vector_knowledge
  FOR EACH ROW EXECUTE FUNCTION guard_vector_content();
CREATE TRIGGER vector_delete_immutable BEFORE DELETE OR TRUNCATE ON vector_knowledge
  FOR EACH STATEMENT EXECUTE FUNCTION reject_ledger_mutation();

DO $$ DECLARE relation_name text; BEGIN
  FOREACH relation_name IN ARRAY ARRAY['memory_events','ledger_constraints','vector_knowledge','ledger_outbox'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',relation_name);
    EXECUTE format('CREATE POLICY scoped_read ON public.%I FOR SELECT TO hivemind_app,hivemind_ledger_writer USING(tenant_id=public.authenticated_tenant_id() AND public.can_access_project(project_id,false))',relation_name);
    EXECUTE format('CREATE POLICY scoped_insert ON public.%I FOR INSERT TO hivemind_ledger_writer WITH CHECK(tenant_id=public.authenticated_tenant_id() AND public.can_access_project(project_id,true))',relation_name);
    EXECUTE format('GRANT SELECT ON public.%I TO hivemind_app,hivemind_ledger_writer',relation_name);
    EXECUTE format('GRANT INSERT ON public.%I TO hivemind_ledger_writer',relation_name);
    -- A NOLOGIN role reachable only through fenced, input-bounded job functions.
    EXECUTE format('CREATE POLICY worker_read ON public.%I FOR SELECT TO hivemind_jobrunner USING(true)',relation_name);
    EXECUTE format('GRANT SELECT ON public.%I TO hivemind_jobrunner',relation_name);
  END LOOP;
END $$;
CREATE POLICY event_actor_check ON memory_events AS RESTRICTIVE FOR INSERT TO hivemind_ledger_writer
  WITH CHECK(actor_key_id=NULLIF(current_setting('app.api_key_id',true),'')::uuid);
CREATE POLICY extraction_insert ON ledger_constraints FOR INSERT TO hivemind_jobrunner
  WITH CHECK(origin='extractor' AND state='tentative');
CREATE POLICY worker_vector_insert ON vector_knowledge FOR INSERT TO hivemind_jobrunner WITH CHECK(true);
CREATE POLICY worker_vector_update ON vector_knowledge FOR UPDATE TO hivemind_jobrunner USING(true) WITH CHECK(true);
CREATE POLICY worker_outbox_write ON ledger_outbox TO hivemind_jobrunner USING(true) WITH CHECK(true);
GRANT INSERT ON ledger_constraints,vector_knowledge,ledger_outbox TO hivemind_jobrunner;
GRANT UPDATE(embedding,embedding_model) ON vector_knowledge TO hivemind_jobrunner;
GRANT UPDATE(status,attempts,available_at,lease_until,lease_token,last_error,result,completed_at) ON ledger_outbox TO hivemind_jobrunner;
CREATE POLICY ledger_writer_tenant ON tenants FOR SELECT TO hivemind_ledger_writer USING(id=authenticated_tenant_id());
CREATE POLICY ledger_writer_project ON projects FOR SELECT TO hivemind_ledger_writer USING(can_access_project(id,false));
CREATE POLICY ledger_writer_usage ON usage_counters TO hivemind_ledger_writer
  USING(tenant_id=authenticated_tenant_id()) WITH CHECK(tenant_id=authenticated_tenant_id());
GRANT SELECT ON tenants,projects TO hivemind_ledger_writer;
GRANT SELECT,INSERT,UPDATE ON usage_counters TO hivemind_ledger_writer;

CREATE FUNCTION resolve_authorized_project(p_project_id uuid DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE resolved uuid; candidate_count integer;
BEGIN
  IF public.authenticated_tenant_id() IS NULL THEN RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000'; END IF;
  IF p_project_id IS NOT NULL THEN
    IF NOT public.can_access_project(p_project_id,false) THEN RAISE EXCEPTION 'project_access_denied' USING ERRCODE='42501'; END IF;
    RETURN p_project_id;
  END IF;
  SELECT count(*),min(p.id::text)::uuid INTO candidate_count,resolved FROM public.projects p;
  IF candidate_count=0 THEN RAISE EXCEPTION 'no_authorized_project' USING ERRCODE='42501'; END IF;
  IF candidate_count>1 THEN RAISE EXCEPTION 'project_required_for_multi_project_key' USING ERRCODE='22023'; END IF;
  RETURN resolved;
END $$;

-- Private helper, granted only to the two NOLOGIN implementation roles.
CREATE FUNCTION append_ledger_claim(p_tenant_id uuid,p_project_id uuid,p_event_id uuid,
  p_claim jsonb,p_origin text,p_model text DEFAULT NULL) RETURNS boolean
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE entity text; claim_state text; claim_kind text; digest_value text;
  head public.ledger_constraints%ROWTYPE; duplicate_head public.ledger_constraints%ROWTYPE;
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
  SELECT * INTO head FROM public.ledger_constraints l
    WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity ORDER BY l.version DESC LIMIT 1;
  IF head.id IS NOT NULL AND head.kind<>claim_kind THEN
    RAISE EXCEPTION 'entity_kind_conflict:%',entity USING ERRCODE='23514';
  END IF;
  digest_value:=public.ledger_digest(jsonb_build_object('entity_key',entity,'kind',claim_kind,'state',claim_state,'value',p_claim->'value'));
  -- An exact repeat of the current authority/proposal is a no-op even with a stale
  -- expected_version. Matching an older historical value is NOT a no-op: a revert
  -- is a new transition and must satisfy optimistic locking against the live head.
  SELECT * INTO duplicate_head FROM public.ledger_constraints l
    WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity
      AND ((claim_state='tentative' AND l.state='tentative') OR (claim_state<>'tentative' AND l.state<>'tentative'))
    ORDER BY l.version DESC LIMIT 1;
  IF duplicate_head.content_digest=digest_value THEN RETURN false; END IF;
  IF COALESCE(head.version,0)<>(p_claim->>'expected_version')::integer THEN
    RAISE EXCEPTION 'ledger_version_conflict:%:current=%',entity,COALESCE(head.version,0) USING ERRCODE='40001';
  END IF;
  IF claim_state='retracted' AND NOT EXISTS(
    SELECT 1 FROM public.ledger_constraints l WHERE l.tenant_id=p_tenant_id AND l.project_id=p_project_id AND l.entity_key=entity
  ) THEN RAISE EXCEPTION 'cannot_retract_missing_entity' USING ERRCODE='23514'; END IF;
  next_version:=COALESCE(head.version,0)+1;
  INSERT INTO public.ledger_constraints(tenant_id,project_id,entity_key,kind,state,value,version,expected_version,
    content_digest,event_id,evidence,origin,extraction_model)
  VALUES(p_tenant_id,p_project_id,entity,claim_kind,claim_state,public.ledger_canonical_json(p_claim->'value'),
    next_version,next_version-1,digest_value,p_event_id,p_claim->>'evidence',p_origin,p_model) RETURNING id INTO new_ledger_id;
  content_value:=entity||' = '||(p_claim->'value')::text;
  INSERT INTO public.vector_knowledge(tenant_id,project_id,event_id,ledger_id,kind,state_at_capture,content,content_digest)
  VALUES(p_tenant_id,p_project_id,p_event_id,new_ledger_id,claim_kind,claim_state,content_value,digest_value)
  ON CONFLICT(tenant_id,project_id,content_digest) DO NOTHING RETURNING id INTO new_memory_id;
  IF new_memory_id IS NOT NULL THEN
    INSERT INTO public.ledger_outbox(tenant_id,project_id,event_id,memory_id,job_kind)
      VALUES(p_tenant_id,p_project_id,p_event_id,new_memory_id,'embedding');
  END IF;
  RETURN true;
END $$;

CREATE FUNCTION validate_ledger_budget(p_project_id uuid) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE accepted_count bigint; accepted_bytes bigint;
BEGIN
  IF (SELECT count(DISTINCT entity_key) FROM public.ledger_constraints WHERE project_id=p_project_id)>128 THEN
    RAISE EXCEPTION 'ledger_key_budget_exceeded:128' USING ERRCODE='23514';
  END IF;
  SELECT count(*),COALESCE(sum(octet_length(entity_key)+octet_length(value::text)+octet_length(evidence)),0)
    INTO accepted_count,accepted_bytes FROM (
      SELECT DISTINCT ON(entity_key) entity_key,value,state,evidence FROM public.ledger_constraints
      WHERE project_id=p_project_id AND state IN ('accepted','retracted') ORDER BY entity_key,version DESC
    ) authoritative WHERE state='accepted';
  IF accepted_count>32 OR accepted_bytes>16384 THEN
    RAISE EXCEPTION 'accepted_ledger_budget_exceeded:32_items_or_16384_bytes' USING ERRCODE='23514';
  END IF;
END $$;

CREATE FUNCTION execute_autonomous_sync(p_project_id uuid,p_query_text text,
  p_query_vector vector(1536),p_new_claims jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project_uuid uuid; tenant_uuid uuid; event_uuid uuid; memory_uuid uuid; event_row public.memory_events%ROWTYPE;
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
    SELECT * INTO event_row FROM public.memory_events
      WHERE tenant_id=tenant_uuid AND project_id=project_uuid AND idempotency_key=(p_new_claims->>'idempotency_key')::uuid;
    IF FOUND THEN
      IF event_row.content_digest<>request_hash THEN RAISE EXCEPTION 'idempotency_conflict' USING ERRCODE='23505'; END IF;
      event_uuid:=event_row.id; replayed:=true;
    ELSE
      PERFORM public.charge_usage('commits');
      INSERT INTO public.memory_events(tenant_id,project_id,actor_key_id,idempotency_key,content_digest,source,fragment,payload)
      VALUES(tenant_uuid,project_uuid,NULLIF(current_setting('app.api_key_id',true),'')::uuid,
        (p_new_claims->>'idempotency_key')::uuid,request_hash,source_value,fragment_value,p_new_claims) RETURNING id INTO event_uuid;
      FOR item IN SELECT * FROM jsonb_array_elements(claims_array) LOOP
        IF public.append_ledger_claim(tenant_uuid,project_uuid,event_uuid,item,'host',NULL) THEN claim_count:=claim_count+1;
        ELSE duplicate_count:=duplicate_count+1; END IF;
      END LOOP;
      IF fragment_value IS NOT NULL THEN
        INSERT INTO public.ledger_outbox(tenant_id,project_id,event_id,job_kind)
          VALUES(tenant_uuid,project_uuid,event_uuid,'extraction');
        INSERT INTO public.vector_knowledge(tenant_id,project_id,event_id,kind,state_at_capture,content,content_digest)
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
      SELECT DISTINCT ON(entity_key) * FROM public.ledger_constraints
      WHERE project_id=project_uuid AND state IN ('accepted','retracted') ORDER BY entity_key,version DESC
    ) latest WHERE state='accepted';
  SELECT COALESCE(jsonb_agg(c),'[]'::jsonb) INTO constraints_context
    FROM jsonb_array_elements(exact_context) c WHERE c->>'kind'='constraint';
  SELECT COALESCE(jsonb_object_agg(entity_key,version),'{}'::jsonb) INTO versions FROM (
    SELECT entity_key,max(version) AS version FROM public.ledger_constraints WHERE project_id=project_uuid GROUP BY entity_key
  ) heads;
  ts_query:=websearch_to_tsquery('simple'::regconfig,p_query_text);
  -- Reciprocal-rank fusion retains exact-term candidates when an embedding is
  -- unavailable. The distance ORDER BY is index-compatible; HNSW is approximate.
  WITH ann_candidates AS MATERIALIZED (
    SELECT id,embedding OPERATOR(public.<=>) p_query_vector AS distance FROM public.vector_knowledge
    WHERE project_id=project_uuid AND embedding IS NOT NULL AND p_query_vector IS NOT NULL
    ORDER BY embedding OPERATOR(public.<=>) p_query_vector LIMIT 40
  ), authorized_vectors AS MATERIALIZED (
    -- Materialize the authorized project BEFORE ordering, preventing another ANN
    -- plan from repeating the underfilled scan. This exact fallback scans project
    -- vectors only, and remains bounded by the runtime statement timeout.
    SELECT id,embedding FROM public.vector_knowledge
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
    SELECT id,ts_rank_cd(search_document,ts_query) AS score FROM public.vector_knowledge
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
      'retrieval_score',f.score,'created_at',v.created_at) ORDER BY f.score DESC,v.id),'[]'::jsonb),
      (SELECT count(*)::integer FROM ann_candidates),
      p_query_vector IS NOT NULL AND (SELECT count(*) FROM ann_candidates)<8
    INTO memories,ann_count,used_semantic_fallback FROM fused f JOIN public.vector_knowledge v ON v.id=f.id;
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

CREATE FUNCTION query_ledger_history(p_project_id uuid,p_entity_key text DEFAULT NULL,
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
    FROM public.ledger_constraints WHERE project_id=resolved AND (p_entity_key IS NULL OR entity_key=p_entity_key)
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

-- Reserve paid query attempts in a separately committed transaction BEFORE the
-- provider call. The sync transaction can roll back without refunding provider spend.
ALTER TABLE usage_counters DROP CONSTRAINT usage_counters_metric_check;
ALTER TABLE usage_counters ADD CONSTRAINT usage_counters_metric_check
  CHECK(metric IN ('commits','recalls','embedding_tokens','query_embedding_attempts'));
CREATE FUNCTION reserve_query_embedding_attempt() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE tenant_uuid uuid:=public.authenticated_tenant_id(); tenant_plan text; used bigint; cap bigint;
BEGIN
  IF tenant_uuid IS NULL THEN RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000'; END IF;
  SELECT plan INTO tenant_plan FROM public.tenants WHERE id=tenant_uuid;
  cap:=CASE WHEN tenant_plan='starter' THEN 25000 ELSE 100000 END;
  INSERT INTO public.usage_counters(tenant_id,period_start,metric,value)
    VALUES(tenant_uuid,date_trunc('month',now() AT TIME ZONE 'UTC')::date,'query_embedding_attempts',1)
    ON CONFLICT(tenant_id,period_start,metric) DO UPDATE SET value=public.usage_counters.value+1
    RETURNING value INTO used;
  IF used>cap THEN RAISE EXCEPTION 'monthly_query_embedding_attempt_limit' USING ERRCODE='P0001'; END IF;
END $$;

-- Only the NOLOGIN auth role can inspect key records; workers get a boolean.
CREATE FUNCTION ledger_actor_can_write(p_key_id uuid,p_project_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
  SELECT EXISTS(SELECT 1 FROM public.api_keys k JOIN public.tenants t ON t.id=k.tenant_id
    JOIN public.projects p ON p.tenant_id=k.tenant_id AND p.id=p_project_id
    WHERE k.id=p_key_id AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>statement_timestamp())
      AND t.billing_status IN ('active','trialing') AND 'memory:write'=ANY(k.scopes)
      AND (k.all_projects OR EXISTS(SELECT 1 FROM public.api_key_projects kp
        WHERE kp.tenant_id=k.tenant_id AND kp.api_key_id=k.id AND kp.project_id=p.id AND kp.can_write)))
$$;
CREATE FUNCTION claim_ledger_jobs(p_limit integer DEFAULT 1)
RETURNS TABLE(job_id uuid,lease_token uuid,job_kind text,event_id uuid,memory_id uuid,content text,attempts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION 'invalid_job_limit' USING ERRCODE='22023'; END IF;
  WITH stale AS (
    SELECT j.id FROM public.ledger_outbox j JOIN public.memory_events e ON e.id=j.event_id
    WHERE (j.status='queued' OR (j.status='running' AND j.lease_until<clock_timestamp()))
      AND (j.attempts>=5 OR NOT public.ledger_actor_can_write(e.actor_key_id,j.project_id))
    ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 100
  ) UPDATE public.ledger_outbox j SET status=CASE WHEN j.attempts>=5 THEN 'dead' ELSE 'canceled' END,
      last_error=CASE WHEN j.attempts>=5 THEN 'retry_budget_exhausted' ELSE 'source_key_not_authorized' END,
      lease_token=NULL,lease_until=NULL,completed_at=clock_timestamp()
    FROM stale WHERE j.id=stale.id;
  RETURN QUERY WITH candidates AS (
    SELECT j.id FROM public.ledger_outbox j JOIN public.memory_events e ON e.id=j.event_id
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
    FROM claimed c JOIN public.memory_events e ON e.id=c.event_id
    LEFT JOIN public.vector_knowledge v ON v.id=c.memory_id;
END $$;
CREATE FUNCTION finish_ledger_embedding_job(p_job_id uuid,p_lease_token uuid,p_embedding vector(1536),p_model text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.ledger_outbox%ROWTYPE; actor uuid;
BEGIN
  IF p_embedding IS NULL OR public.vector_dims(p_embedding)<>1536 OR public.vector_norm(p_embedding)=0
    OR p_model IS DISTINCT FROM 'text-embedding-3-small' THEN RAISE EXCEPTION 'invalid_embedding' USING ERRCODE='22023'; END IF;
  SELECT * INTO job FROM public.ledger_outbox WHERE id=p_job_id AND lease_token=p_lease_token
    AND job_kind='embedding' AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  SELECT actor_key_id INTO actor FROM public.memory_events WHERE id=job.event_id;
  IF NOT public.ledger_actor_can_write(actor,job.project_id) THEN
    UPDATE public.ledger_outbox SET status='canceled',last_error='source_key_not_authorized',
      lease_until=NULL,lease_token=NULL,completed_at=clock_timestamp() WHERE id=job.id;
    RETURN false;
  END IF;
  UPDATE public.vector_knowledge SET embedding=p_embedding,embedding_model=p_model
    WHERE id=job.memory_id AND tenant_id=job.tenant_id AND project_id=job.project_id AND embedding IS NULL;
  UPDATE public.ledger_outbox SET status='done',lease_until=NULL,lease_token=NULL,last_error=NULL,
    result=jsonb_build_object('model',p_model),completed_at=clock_timestamp() WHERE id=job.id;
  RETURN true;
END $$;
CREATE FUNCTION finish_ledger_extraction_job(p_job_id uuid,p_lease_token uuid,p_claims jsonb,p_model text)
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
  SELECT actor_key_id INTO actor FROM public.memory_events WHERE id=job.event_id;
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
    SELECT COALESCE(max(version),0) INTO live_version FROM public.ledger_constraints
      WHERE tenant_id=job.tenant_id AND project_id=job.project_id AND entity_key=normalized_entity;
    item:=jsonb_set(item,'{expected_version}',to_jsonb(live_version));
    IF public.append_ledger_claim(job.tenant_id,job.project_id,job.event_id,item,'extractor',p_model) THEN inserted:=inserted+1; END IF;
  END LOOP;
  PERFORM public.validate_ledger_budget(job.project_id);
  UPDATE public.ledger_outbox SET status='done',lease_until=NULL,lease_token=NULL,last_error=NULL,
    result=jsonb_build_object('model',p_model,'claims_inserted',inserted),completed_at=clock_timestamp() WHERE id=job.id;
  RETURN true;
END $$;
CREATE FUNCTION fail_ledger_job(p_job_id uuid,p_lease_token uuid,p_error_code text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.ledger_outbox%ROWTYPE;
BEGIN
  IF p_error_code IS NULL OR p_error_code !~ '^[a-zA-Z0-9_.:-]{1,120}$' THEN
    RAISE EXCEPTION 'invalid_error_code' USING ERRCODE='22023';
  END IF;
  SELECT * INTO job FROM public.ledger_outbox WHERE id=p_job_id AND lease_token=p_lease_token
    AND status='running' AND lease_until>clock_timestamp() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE public.ledger_outbox SET status=CASE WHEN attempts>=5 THEN 'dead' ELSE 'queued' END,
    available_at=clock_timestamp()+make_interval(secs=>LEAST(300,5*power(2,attempts)::integer)+floor(random()*5)::integer),
    lease_until=NULL,lease_token=NULL,last_error=p_error_code,
    completed_at=CASE WHEN attempts>=5 THEN clock_timestamp() ELSE NULL END WHERE id=job.id;
  RETURN true;
END $$;
RESET ROLE;

ALTER FUNCTION reserve_query_embedding_attempt() OWNER TO hivemind_ledger_writer;
ALTER FUNCTION resolve_authorized_project(uuid) OWNER TO hivemind_ledger_writer;
ALTER FUNCTION execute_autonomous_sync(uuid,text,vector,jsonb) OWNER TO hivemind_ledger_writer;
ALTER FUNCTION query_ledger_history(uuid,text,integer,integer) OWNER TO hivemind_ledger_writer;
ALTER FUNCTION ledger_actor_can_write(uuid,uuid) OWNER TO hivemind_auth;
ALTER FUNCTION claim_ledger_jobs(integer) OWNER TO hivemind_jobrunner;
ALTER FUNCTION finish_ledger_embedding_job(uuid,uuid,vector,text) OWNER TO hivemind_jobrunner;
ALTER FUNCTION finish_ledger_extraction_job(uuid,uuid,jsonb,text) OWNER TO hivemind_jobrunner;
ALTER FUNCTION fail_ledger_job(uuid,uuid,text) OWNER TO hivemind_jobrunner;
REVOKE ALL ON FUNCTION reserve_query_embedding_attempt(),ledger_canonical_json(jsonb),ledger_digest(jsonb),reject_ledger_mutation(),guard_vector_content(),
  resolve_authorized_project(uuid),append_ledger_claim(uuid,uuid,uuid,jsonb,text,text),validate_ledger_budget(uuid),
  execute_autonomous_sync(uuid,text,vector,jsonb),query_ledger_history(uuid,text,integer,integer),ledger_actor_can_write(uuid,uuid),
  claim_ledger_jobs(integer),finish_ledger_embedding_job(uuid,uuid,vector,text),finish_ledger_extraction_job(uuid,uuid,jsonb,text),
  fail_ledger_job(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authenticated_tenant_id(),can_access_project(uuid,boolean),charge_usage(text)
  TO hivemind_ledger_writer;
GRANT EXECUTE ON FUNCTION ledger_canonical_json(jsonb),ledger_digest(jsonb),
  append_ledger_claim(uuid,uuid,uuid,jsonb,text,text),validate_ledger_budget(uuid)
  TO hivemind_ledger_writer,hivemind_jobrunner;
GRANT EXECUTE ON FUNCTION reserve_query_embedding_attempt(),resolve_authorized_project(uuid),execute_autonomous_sync(uuid,text,vector,jsonb),
  query_ledger_history(uuid,text,integer,integer) TO hivemind_app;
GRANT EXECUTE ON FUNCTION ledger_actor_can_write(uuid,uuid) TO hivemind_jobrunner;
GRANT EXECUTE ON FUNCTION claim_ledger_jobs(integer),finish_ledger_embedding_job(uuid,uuid,vector,text),
  finish_ledger_extraction_job(uuid,uuid,jsonb,text),fail_ledger_job(uuid,uuid,text) TO hivemind_worker;
