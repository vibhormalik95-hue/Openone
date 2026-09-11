-- Execute with psql --single-transaction; migration runner records checksum atomically.
CREATE EXTENSION IF NOT EXISTS vector;
DO $$ BEGIN
  IF current_setting('server_version_num')::integer < 170000 THEN
    RAISE EXCEPTION 'PostgreSQL 17 or newer required';
  END IF;
  IF (SELECT string_to_array(extversion, '.')::int[] < ARRAY[0,8,0] FROM pg_extension WHERE extname='vector') THEN
    RAISE EXCEPTION 'pgvector >=0.8.0 required for iterative scans';
  END IF;
END $$;
SET LOCAL ROLE hivemind_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE TABLE tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  plan text NOT NULL DEFAULT 'starter' CHECK (plan IN ('starter','pro')),
  billing_status text NOT NULL DEFAULT 'incomplete' CHECK (billing_status IN ('active','trialing','past_due','unpaid','canceled','incomplete','paused','incomplete_expired')),
  stripe_customer_id text UNIQUE,
  stripe_subscription_id text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE tenant_memberships (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 255),
  role text NOT NULL CHECK (role IN ('owner','admin','member')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id,subject), UNIQUE(tenant_id,id)
);
CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  slug text NOT NULL CHECK (length(slug) <= 80 AND slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,slug), UNIQUE(tenant_id,id)
);
CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  member_id uuid,
  key_hash text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
  prefix text NOT NULL CHECK (length(prefix) BETWEEN 4 AND 20),
  label text NOT NULL DEFAULT 'personal' CHECK (length(label) BETWEEN 1 AND 120),
  all_projects boolean NOT NULL DEFAULT false,
  scopes text[] NOT NULL DEFAULT ARRAY['memory:read','memory:write'] CHECK (scopes <@ ARRAY['memory:read','memory:write']::text[] AND cardinality(scopes)>0 AND ('memory:read'=ANY(scopes))),
  expires_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,id),
  FOREIGN KEY (tenant_id,member_id) REFERENCES tenant_memberships(tenant_id,id)
);
CREATE TABLE api_key_projects (
  tenant_id uuid NOT NULL,
  api_key_id uuid NOT NULL,
  project_id uuid NOT NULL,
  can_write boolean NOT NULL DEFAULT true,
  PRIMARY KEY(api_key_id,project_id),
  FOREIGN KEY(tenant_id,api_key_id) REFERENCES api_keys(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id) ON DELETE CASCADE
);
CREATE INDEX api_key_projects_project ON api_key_projects(tenant_id,project_id);
CREATE TABLE conversation_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  actor_key_id uuid NOT NULL,
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  payload_hash text NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object' AND octet_length(payload::text)<=131072),
  source jsonb NOT NULL CHECK (jsonb_typeof(source)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,project_id,idempotency_key),
  UNIQUE(tenant_id,project_id,id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id) ON DELETE CASCADE,
  FOREIGN KEY(tenant_id,actor_key_id) REFERENCES api_keys(tenant_id,id)
);
CREATE INDEX conversation_events_recent ON conversation_events(tenant_id,project_id,created_at DESC);
CREATE TABLE constraints_ledger (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  key text NOT NULL CHECK(length(key) BETWEEN 1 AND 96 AND key ~ '^[a-z0-9_.-]+$'),
  value jsonb NOT NULL CHECK(octet_length(value::text)<=2048),
  version integer NOT NULL CHECK(version>0),
  active boolean NOT NULL DEFAULT true,
  event_id uuid NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,project_id,key,version),
  FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES conversation_events(tenant_id,project_id,id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX constraints_one_active ON constraints_ledger(tenant_id,project_id,key) WHERE active;
CREATE INDEX constraints_versions ON constraints_ledger(tenant_id,project_id,key,version DESC);
CREATE TABLE memory_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  event_id uuid NOT NULL,
  kind text NOT NULL CHECK(kind IN ('decision','task','state','note')),
  content text NOT NULL CHECK(length(content) BETWEEN 1 AND 4000),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(metadata)='object' AND octet_length(metadata::text)<=2048),
  content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
  embedding vector(1536),
  embedding_model text NOT NULL DEFAULT 'text-embedding-3-small' CHECK(embedding_model='text-embedding-3-small'),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,project_id,content_hash), UNIQUE(tenant_id,project_id,id),
  FOREIGN KEY(tenant_id,project_id,event_id) REFERENCES conversation_events(tenant_id,project_id,id) ON DELETE CASCADE,
  CHECK (embedding IS NULL OR vector_norm(embedding)>0)
);
CREATE INDEX memory_project_recent ON memory_embeddings(tenant_id,project_id,created_at DESC);
CREATE INDEX memory_cosine_hnsw ON memory_embeddings USING hnsw(embedding vector_cosine_ops) WITH(m=16,ef_construction=64);
CREATE TABLE embedding_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL,
  project_id uuid NOT NULL,
  memory_id uuid NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed')),
  attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
  available_at timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  lease_token uuid,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(tenant_id,project_id,memory_id) REFERENCES memory_embeddings(tenant_id,project_id,id) ON DELETE CASCADE
);
CREATE INDEX embedding_jobs_ready ON embedding_jobs(available_at,created_at) WHERE status='queued';
CREATE INDEX embedding_jobs_expired ON embedding_jobs(lease_until) WHERE status='running';
CREATE TABLE usage_counters (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  period_start date NOT NULL,
  metric text NOT NULL CHECK(metric IN ('commits','recalls','embedding_tokens')),
  value bigint NOT NULL DEFAULT 0 CHECK(value>=0),
  PRIMARY KEY(tenant_id,period_start,metric)
);
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['tenants','tenant_memberships','projects','api_keys','api_key_projects','conversation_events','constraints_ledger','memory_embeddings','embedding_jobs','usage_counters'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['tenants','tenant_memberships','projects','api_keys','api_key_projects'] LOOP
    EXECUTE format('CREATE POLICY billing_control ON public.%I TO hivemind_billing USING(true) WITH CHECK(true)',t);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON public.%I TO hivemind_billing',t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['tenants','projects','api_keys','api_key_projects'] LOOP
    EXECUTE format('CREATE POLICY auth_lookup ON public.%I FOR SELECT TO hivemind_auth USING(true)',t);
    EXECUTE format('GRANT SELECT ON public.%I TO hivemind_auth',t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['memory_embeddings','embedding_jobs'] LOOP
    EXECUTE format('CREATE POLICY job_process ON public.%I TO hivemind_jobrunner USING(true) WITH CHECK(true)',t);
    EXECUTE format('GRANT SELECT,UPDATE ON public.%I TO hivemind_jobrunner',t);
  END LOOP;
END $$;
RESET ROLE;
