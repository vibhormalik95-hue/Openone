SET ROLE hivemind_owner;
CREATE TABLE oauth_identity_bindings (
  issuer text NOT NULL CHECK(length(issuer) BETWEEN 10 AND 512 AND issuer LIKE 'https://%/'),
  subject text NOT NULL CHECK(length(subject) BETWEEN 1 AND 255),
  key_id uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  PRIMARY KEY(issuer,subject)
);
CREATE INDEX oauth_binding_key ON oauth_identity_bindings(key_id);
ALTER TABLE oauth_identity_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_identity_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_control ON oauth_identity_bindings TO hivemind_billing
  USING(true) WITH CHECK(true);
GRANT SELECT,INSERT,UPDATE,DELETE ON oauth_identity_bindings TO hivemind_billing;
CREATE POLICY auth_lookup ON oauth_identity_bindings FOR SELECT TO hivemind_auth USING(true);
GRANT SELECT ON oauth_identity_bindings TO hivemind_auth;
CREATE FUNCTION lookup_oauth_identity(p_issuer text,p_subject text)
RETURNS TABLE(key_hash text) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
  SELECT k.key_hash FROM public.oauth_identity_bindings b
  JOIN public.api_keys k ON k.id=b.key_id
  JOIN public.tenants t ON t.id=k.tenant_id
  WHERE b.issuer=p_issuer AND b.subject=p_subject AND b.revoked_at IS NULL
    AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>now())
    AND t.billing_status IN ('active','trialing');
$$;
RESET ROLE;
ALTER FUNCTION public.lookup_oauth_identity(text,text) OWNER TO hivemind_auth;
REVOKE ALL ON FUNCTION public.lookup_oauth_identity(text,text) FROM PUBLIC;
-- Identity resolution belongs to the trusted control plane, never memory SQL.
REVOKE ALL ON FUNCTION public.lookup_oauth_identity(text,text) FROM hivemind_app;
GRANT EXECUTE ON FUNCTION public.lookup_oauth_identity(text,text) TO hivemind_billing;
RESET ROLE;
