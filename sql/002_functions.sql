-- Execute with psql --single-transaction; migration runner records checksum atomically.
SET ROLE hivemind_owner;
CREATE FUNCTION authenticate_api_key(p_key_hash text)
RETURNS TABLE(key_id uuid,tenant_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE k public.api_keys%ROWTYPE;
BEGIN
  -- Both proof and identifier are transaction-local; a key UUID alone is insufficient.
  PERFORM set_config('app.api_key_id','',true);
  PERFORM set_config('app.api_key_hash','',true);
  IF p_key_hash IS NULL OR p_key_hash !~ '^[0-9a-f]{64}$' THEN
    RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000';
  END IF;
  SELECT a.* INTO k FROM public.api_keys a JOIN public.tenants t ON t.id=a.tenant_id
  WHERE a.key_hash=p_key_hash AND a.revoked_at IS NULL
    AND (a.expires_at IS NULL OR a.expires_at>statement_timestamp())
    AND t.billing_status IN ('active','trialing');
  IF NOT FOUND THEN RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000'; END IF;
  PERFORM set_config('app.api_key_id',k.id::text,true);
  PERFORM set_config('app.api_key_hash',p_key_hash,true);
  RETURN QUERY SELECT k.id,k.tenant_id;
END $$;
CREATE FUNCTION authenticated_tenant_id() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
  SELECT a.tenant_id FROM public.api_keys a JOIN public.tenants t ON t.id=a.tenant_id
  WHERE a.id=NULLIF(current_setting('app.api_key_id',true),'')::uuid
    AND a.key_hash=NULLIF(current_setting('app.api_key_hash',true),'')
    AND a.revoked_at IS NULL AND (a.expires_at IS NULL OR a.expires_at>statement_timestamp())
    AND t.billing_status IN ('active','trialing')
$$;
CREATE FUNCTION can_access_project(p_project_id uuid,p_require_write boolean DEFAULT false) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.api_keys k JOIN public.tenants t ON t.id=k.tenant_id
    JOIN public.projects p ON p.tenant_id=k.tenant_id AND p.id=p_project_id
    WHERE k.id=NULLIF(current_setting('app.api_key_id',true),'')::uuid
      AND k.key_hash=NULLIF(current_setting('app.api_key_hash',true),'')
      AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>statement_timestamp())
      AND t.billing_status IN ('active','trialing')
      AND (CASE WHEN p_require_write THEN 'memory:write' ELSE 'memory:read' END)=ANY(k.scopes)
      AND (k.all_projects OR EXISTS (
        SELECT 1 FROM public.api_key_projects kp WHERE kp.api_key_id=k.id
          AND kp.tenant_id=k.tenant_id AND kp.project_id=p.id
          AND (NOT p_require_write OR kp.can_write)
      ))
  )
$$;
CREATE POLICY tenant_read ON tenants FOR SELECT TO hivemind_app USING(id=authenticated_tenant_id());
CREATE POLICY project_read ON projects FOR SELECT TO hivemind_app USING(can_access_project(id,false));
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['conversation_events','constraints_ledger','memory_embeddings','embedding_jobs'] LOOP
    EXECUTE format('CREATE POLICY scoped_read ON public.%I FOR SELECT TO hivemind_app USING(tenant_id=public.authenticated_tenant_id() AND public.can_access_project(project_id,false))',t);
    EXECUTE format('CREATE POLICY scoped_insert ON public.%I FOR INSERT TO hivemind_app WITH CHECK(tenant_id=public.authenticated_tenant_id() AND public.can_access_project(project_id,true))',t);
    EXECUTE format('GRANT SELECT,INSERT ON public.%I TO hivemind_app',t);
  END LOOP;
END $$;
CREATE POLICY event_actor_insert ON conversation_events AS RESTRICTIVE FOR INSERT TO hivemind_app
  WITH CHECK(actor_key_id=NULLIF(current_setting('app.api_key_id',true),'')::uuid);
CREATE POLICY constraint_update ON constraints_ledger FOR UPDATE TO hivemind_app
  USING(tenant_id=authenticated_tenant_id() AND can_access_project(project_id,true))
  WITH CHECK(tenant_id=authenticated_tenant_id() AND can_access_project(project_id,true));
GRANT UPDATE(active) ON constraints_ledger TO hivemind_app;
GRANT SELECT ON projects,tenants TO hivemind_app;
CREATE POLICY usage_scoped ON usage_counters TO hivemind_app
  USING(tenant_id=authenticated_tenant_id()) WITH CHECK(tenant_id=authenticated_tenant_id());
GRANT SELECT,INSERT,UPDATE ON usage_counters TO hivemind_app;
CREATE FUNCTION enforce_project_limit() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p text;
BEGIN
  IF TG_OP='UPDATE' AND NEW.tenant_id=OLD.tenant_id THEN RETURN NEW; END IF;
  -- The tenant lock serializes concurrent inserts from every provisioning replica.
  SELECT plan INTO p FROM public.tenants WHERE id=NEW.tenant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'tenant_missing'; END IF;
  IF p='starter' AND (SELECT count(*) FROM public.projects WHERE tenant_id=NEW.tenant_id)>=5 THEN
    RAISE EXCEPTION 'starter_project_limit' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER projects_limit BEFORE INSERT OR UPDATE OF tenant_id ON projects FOR EACH ROW EXECUTE FUNCTION enforce_project_limit();
CREATE FUNCTION charge_usage(p_metric text) RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE t uuid:=public.authenticated_tenant_id(); p text; used bigint; cap bigint;
BEGIN
  IF t IS NULL THEN RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000'; END IF;
  SELECT plan INTO p FROM public.tenants WHERE id=t;
  IF p_metric='commits' THEN cap:=CASE WHEN p='starter' THEN 5000 ELSE 20000 END;
  ELSIF p_metric='recalls' THEN cap:=CASE WHEN p='starter' THEN 25000 ELSE 100000 END;
  ELSE RAISE EXCEPTION 'invalid_metric' USING ERRCODE='22023'; END IF;
  INSERT INTO public.usage_counters(tenant_id,period_start,metric,value)
    VALUES(t,date_trunc('month',now() AT TIME ZONE 'UTC')::date,p_metric,1)
    ON CONFLICT(tenant_id,period_start,metric) DO UPDATE SET value=public.usage_counters.value+1
    RETURNING value INTO used;
  IF used>cap THEN RAISE EXCEPTION 'monthly_usage_limit' USING ERRCODE='P0001'; END IF;
END $$;
CREATE FUNCTION ensure_recall_allowance() RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE t uuid:=public.authenticated_tenant_id(); p text; used bigint; cap bigint;
BEGIN
  IF t IS NULL THEN RAISE EXCEPTION 'unauthorized' USING ERRCODE='28000'; END IF;
  SELECT plan INTO p FROM public.tenants WHERE id=t;
  cap:=CASE WHEN p='starter' THEN 25000 ELSE 100000 END;
  SELECT value INTO used FROM public.usage_counters
    WHERE tenant_id=t AND metric='recalls'
      AND period_start=date_trunc('month',now() AT TIME ZONE 'UTC')::date;
  IF COALESCE(used,0)>=cap THEN RAISE EXCEPTION 'monthly_usage_limit' USING ERRCODE='P0001'; END IF;
END $$;
CREATE FUNCTION commit_project_memory(p_project_id uuid,p_idempotency_key text,p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE
  t uuid:=public.authenticated_tenant_id();
  event_uuid uuid; existing public.conversation_events%ROWTYPE;
  request_hash text; item jsonb; memory_uuid uuid; inserted integer:=0;
  old_version integer; new_version integer; versions jsonb:='{}'::jsonb;
  source_object jsonb; memories jsonb; constraints_array jsonb; constraint_status text;
BEGIN
  IF NOT public.can_access_project(p_project_id,true) THEN RAISE EXCEPTION 'project_access_denied' USING ERRCODE='42501'; END IF;
  IF p_idempotency_key IS NULL OR length(p_idempotency_key) NOT BETWEEN 1 AND 128
     OR p_payload IS NULL OR jsonb_typeof(p_payload)<>'object' OR octet_length(p_payload::text)>131072
  THEN RAISE EXCEPTION 'invalid_commit' USING ERRCODE='22023'; END IF;
  source_object:=p_payload->'source'; memories:=p_payload->'memories'; constraints_array:=p_payload->'constraints';
  IF jsonb_typeof(source_object) IS DISTINCT FROM 'object' OR jsonb_typeof(memories) IS DISTINCT FROM 'array'
     OR jsonb_typeof(constraints_array) IS DISTINCT FROM 'array'
  THEN RAISE EXCEPTION 'invalid_commit_shape' USING ERRCODE='22023'; END IF;
  IF jsonb_array_length(memories)>20 OR jsonb_array_length(constraints_array)>20
     OR jsonb_array_length(memories)+jsonb_array_length(constraints_array)=0
  THEN RAISE EXCEPTION 'invalid_commit_size' USING ERRCODE='22023'; END IF;
  IF (SELECT count(*) FROM jsonb_array_elements(constraints_array) x)
     <> (SELECT count(DISTINCT x->>'key') FROM jsonb_array_elements(constraints_array) x)
  THEN RAISE EXCEPTION 'duplicate_constraint_keys' USING ERRCODE='22023'; END IF;
  request_hash:=encode(sha256(convert_to(p_payload::text,'UTF8')),'hex');
  -- Serialize exact state updates and idempotency within this project, not globally.
  PERFORM pg_advisory_xact_lock(hashtextextended(p_project_id::text,0));
  SELECT * INTO existing FROM public.conversation_events
    WHERE tenant_id=t AND project_id=p_project_id AND idempotency_key=p_idempotency_key;
  IF FOUND THEN
    IF existing.payload_hash<>request_hash THEN RAISE EXCEPTION 'idempotency_conflict' USING ERRCODE='23505'; END IF;
    SELECT COALESCE(jsonb_object_agg(key,version),'{}'::jsonb) INTO versions
      FROM public.constraints_ledger WHERE event_id=existing.id AND project_id=p_project_id;
    RETURN jsonb_build_object('event_id',existing.id,'replayed',true,'memories_inserted',0,
      'constraint_versions',versions,'embedding_status','previously_queued');
  END IF;
  PERFORM public.charge_usage('commits');
  INSERT INTO public.conversation_events(tenant_id,project_id,actor_key_id,idempotency_key,payload_hash,payload,source)
    VALUES(t,p_project_id,NULLIF(current_setting('app.api_key_id',true),'')::uuid,p_idempotency_key,request_hash,p_payload,source_object) RETURNING id INTO event_uuid;
  FOR item IN SELECT * FROM jsonb_array_elements(constraints_array) LOOP
    IF jsonb_typeof(item)<>'object' OR NOT(item ?& ARRAY['key','value','expected_version','status'])
       OR jsonb_typeof(item->'expected_version')<>'number' OR (item->>'expected_version') !~ '^[0-9]+$'
       OR item->>'status' NOT IN ('active','superseded')
    THEN RAISE EXCEPTION 'invalid_constraint' USING ERRCODE='22023'; END IF;
    SELECT COALESCE(max(version),0) INTO old_version FROM public.constraints_ledger
      WHERE tenant_id=t AND project_id=p_project_id AND key=item->>'key';
    IF old_version<>(item->>'expected_version')::integer THEN
      RAISE EXCEPTION 'constraint_version_conflict:%',item->>'key' USING ERRCODE='40001';
    END IF;
    new_version:=old_version+1; constraint_status:=item->>'status';
    UPDATE public.constraints_ledger SET active=false
      WHERE tenant_id=t AND project_id=p_project_id AND key=item->>'key' AND active;
    INSERT INTO public.constraints_ledger(tenant_id,project_id,key,value,version,active,event_id)
      VALUES(t,p_project_id,item->>'key',item->'value',new_version,constraint_status='active',event_uuid);
    versions:=versions||jsonb_build_object(item->>'key',new_version);
  END LOOP;
  -- Every historical key head is returned so fresh clients can reactivate it.
  IF (SELECT count(DISTINCT key) FROM public.constraints_ledger WHERE project_id=p_project_id)>128 THEN
    RAISE EXCEPTION 'constraint_key_budget_exceeded:128_distinct_keys' USING ERRCODE='23514';
  END IF;
  -- Exact requirements must never be silently dropped to fit a context packet.
  IF (SELECT count(*) FROM public.constraints_ledger WHERE project_id=p_project_id AND active)>32
     OR (SELECT COALESCE(sum(octet_length(key)+octet_length(value::text)),0)
         FROM public.constraints_ledger WHERE project_id=p_project_id AND active)>16384
  THEN RAISE EXCEPTION 'active_constraint_budget_exceeded:32_items_or_16384_bytes' USING ERRCODE='23514'; END IF;
  FOR item IN SELECT * FROM jsonb_array_elements(memories) LOOP
    IF jsonb_typeof(item)<>'object' OR jsonb_typeof(item->'text') IS DISTINCT FROM 'string'
      OR jsonb_typeof(item->'kind') IS DISTINCT FROM 'string'
    THEN RAISE EXCEPTION 'invalid_memory' USING ERRCODE='22023'; END IF;
    memory_uuid:=NULL;
    INSERT INTO public.memory_embeddings(tenant_id,project_id,event_id,kind,content,metadata,content_hash)
      VALUES(t,p_project_id,event_uuid,item->>'kind',item->>'text',COALESCE(item->'metadata','{}'::jsonb),
        encode(sha256(convert_to((jsonb_build_object('kind',item->>'kind','text',btrim(item->>'text'),
          'metadata',COALESCE(item->'metadata','{}'::jsonb)))::text,'UTF8')),'hex'))
      ON CONFLICT(tenant_id,project_id,content_hash) DO NOTHING RETURNING id INTO memory_uuid;
    IF memory_uuid IS NOT NULL THEN
      inserted:=inserted+1;
      INSERT INTO public.embedding_jobs(tenant_id,project_id,memory_id) VALUES(t,p_project_id,memory_uuid);
    END IF;
  END LOOP;
  RETURN jsonb_build_object('event_id',event_uuid,'replayed',false,'memories_inserted',inserted,
    'constraint_versions',versions,'embedding_status','queued');
END $$;
CREATE FUNCTION match_project_context(p_project_id uuid,p_query_vector vector(1536),p_limit integer DEFAULT 8)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE exact_context jsonb; semantic_context jsonb; pending_count bigint; exact_bytes bigint; exact_count bigint; version_heads jsonb; key_count bigint;
BEGIN
  IF NOT public.can_access_project(p_project_id,false) THEN RAISE EXCEPTION 'project_access_denied' USING ERRCODE='42501'; END IF;
  IF p_limit IS NULL OR p_limit<1 OR p_limit>20
    OR (p_query_vector IS NOT NULL AND (public.vector_dims(p_query_vector)<>1536 OR public.vector_norm(p_query_vector)=0))
  THEN RAISE EXCEPTION 'invalid_recall_arguments' USING ERRCODE='22023'; END IF;
  PERFORM public.charge_usage('recalls');
  PERFORM set_config('hnsw.ef_search','100',true);
  PERFORM set_config('hnsw.iterative_scan','strict_order',true);
  PERFORM set_config('hnsw.max_scan_tuples','20000',true);
  PERFORM set_config('hnsw.scan_mem_multiplier','2',true);
  SELECT COALESCE(jsonb_agg(jsonb_build_object('key',key,'value',value,'version',version,'updated_at',updated_at) ORDER BY key),'[]'::jsonb),
    count(*),COALESCE(sum(octet_length(key)+octet_length(value::text)),0)
    INTO exact_context,exact_count,exact_bytes FROM public.constraints_ledger WHERE project_id=p_project_id AND active;
  IF exact_count>32 OR exact_bytes>16384 THEN
    RAISE EXCEPTION 'active_constraint_budget_exceeded' USING ERRCODE='23514';
  END IF;
  SELECT COALESCE(jsonb_object_agg(key,version),'{}'::jsonb),count(*)
    INTO version_heads,key_count FROM (
      SELECT key,max(version) AS version FROM public.constraints_ledger
      WHERE project_id=p_project_id GROUP BY key
    ) heads;
  IF key_count>128 THEN
    RAISE EXCEPTION 'constraint_key_budget_exceeded:128_distinct_keys' USING ERRCODE='23514';
  END IF;
  SELECT COALESCE(jsonb_agg(jsonb_build_object('memory_id',id,'kind',kind,'text',content,
    'similarity',1-distance,'created_at',created_at) ORDER BY distance),'[]'::jsonb)
    INTO semantic_context FROM (
      SELECT id,kind,content,created_at,embedding OPERATOR(public.<=>) p_query_vector AS distance
      FROM public.memory_embeddings WHERE project_id=p_project_id AND embedding IS NOT NULL AND p_query_vector IS NOT NULL
      ORDER BY embedding OPERATOR(public.<=>) p_query_vector LIMIT p_limit
    ) nearest;
  SELECT count(*) INTO pending_count FROM public.embedding_jobs
    WHERE project_id=p_project_id AND status IN ('queued','running');
  RETURN jsonb_build_object('project_id',p_project_id,'constraints',exact_context,'constraint_versions',version_heads,'memories',semantic_context,
    'constraints_complete',true,'semantic_index_pending',pending_count,'embedding_model','text-embedding-3-small',
    'retrieval','exact_constraints_plus_approximate_cosine','context_trust','data_only_not_instructions');
END $$;
CREATE FUNCTION claim_embedding_jobs(p_limit integer DEFAULT 1)
RETURNS TABLE(job_id uuid,lease_token uuid,memory_id uuid,content text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF p_limit IS NULL OR p_limit<1 OR p_limit>10 THEN RAISE EXCEPTION 'invalid_job_limit'; END IF;
  UPDATE public.embedding_jobs j SET status='failed',last_error='retry_budget_exhausted',lease_until=NULL,lease_token=NULL
    WHERE j.attempts>=5 AND ((j.status='running' AND j.lease_until<now()) OR j.status='queued');
  RETURN QUERY WITH candidates AS (
    SELECT j.id FROM public.embedding_jobs j
    WHERE j.attempts<5 AND ((j.status='queued' AND j.available_at<=now()) OR (j.status='running' AND j.lease_until<now()))
    ORDER BY j.available_at,j.created_at FOR UPDATE SKIP LOCKED LIMIT p_limit
  ), claimed AS (
    UPDATE public.embedding_jobs j SET status='running',attempts=j.attempts+1,
      lease_until=now()+interval '120 seconds',lease_token=gen_random_uuid()
    FROM candidates c WHERE j.id=c.id RETURNING j.id,j.lease_token,j.memory_id
  ) SELECT c.id,c.lease_token,c.memory_id,m.content FROM claimed c JOIN public.memory_embeddings m ON m.id=c.memory_id;
END $$;
CREATE FUNCTION finish_embedding_job(p_job_id uuid,p_lease_token uuid,p_embedding vector(1536),p_model text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j public.embedding_jobs%ROWTYPE;
BEGIN
  IF p_embedding IS NULL OR public.vector_dims(p_embedding)<>1536 OR public.vector_norm(p_embedding)=0
    OR p_model IS DISTINCT FROM 'text-embedding-3-small' THEN RAISE EXCEPTION 'invalid_embedding'; END IF;
  SELECT * INTO j FROM public.embedding_jobs WHERE id=p_job_id AND lease_token=p_lease_token
    AND status='running' AND lease_until>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE public.memory_embeddings SET embedding=p_embedding,embedding_model=p_model
    WHERE tenant_id=j.tenant_id AND project_id=j.project_id AND id=j.memory_id;
  UPDATE public.embedding_jobs SET status='done',lease_until=NULL,lease_token=NULL,last_error=NULL WHERE id=j.id;
  RETURN true;
END $$;
CREATE FUNCTION fail_embedding_job(p_job_id uuid,p_lease_token uuid,p_error text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE j public.embedding_jobs%ROWTYPE;
BEGIN
  SELECT * INTO j FROM public.embedding_jobs WHERE id=p_job_id AND lease_token=p_lease_token
    AND status='running' AND lease_until>now() FOR UPDATE;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE public.embedding_jobs SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'queued' END,
    available_at=now()+make_interval(secs=>LEAST(300,5*power(2,attempts)::integer)+floor(random()*5)::integer),
    lease_until=NULL,lease_token=NULL,last_error=left(regexp_replace(COALESCE(p_error,'unknown'),'[^a-zA-Z0-9_.:-]','_','g'),120)
    WHERE id=j.id;
  RETURN true;
END $$;
RESET ROLE;
-- Ownership transfer is performed by the migration administrator, not runtime.
ALTER FUNCTION authenticate_api_key(text) OWNER TO hivemind_auth;
ALTER FUNCTION authenticated_tenant_id() OWNER TO hivemind_auth;
ALTER FUNCTION can_access_project(uuid,boolean) OWNER TO hivemind_auth;
ALTER FUNCTION claim_embedding_jobs(integer) OWNER TO hivemind_jobrunner;
ALTER FUNCTION finish_embedding_job(uuid,uuid,vector,text) OWNER TO hivemind_jobrunner;
ALTER FUNCTION fail_embedding_job(uuid,uuid,text) OWNER TO hivemind_jobrunner;
REVOKE ALL ON FUNCTION authenticate_api_key(text),authenticated_tenant_id(),can_access_project(uuid,boolean),charge_usage(text),ensure_recall_allowance(),commit_project_memory(uuid,text,jsonb),match_project_context(uuid,vector,integer),claim_embedding_jobs(integer),finish_embedding_job(uuid,uuid,vector,text),fail_embedding_job(uuid,uuid,text),enforce_project_limit() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authenticate_api_key(text),authenticated_tenant_id(),can_access_project(uuid,boolean),charge_usage(text),ensure_recall_allowance(),commit_project_memory(uuid,text,jsonb),match_project_context(uuid,vector,integer) TO hivemind_app;
GRANT EXECUTE ON FUNCTION claim_embedding_jobs(integer),finish_embedding_job(uuid,uuid,vector,text),fail_embedding_job(uuid,uuid,text) TO hivemind_worker;
