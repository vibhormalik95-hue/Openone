SET ROLE hivemind_owner;
-- Billing control plane: runtime memory role gets no privileges on these tables.
CREATE TABLE checkout_claims (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 nonce_hash text NOT NULL UNIQUE CHECK (length(nonce_hash)=64),
 plan text NOT NULL CHECK (plan IN ('starter','pro')),
 stripe_session_id text UNIQUE,
 tenant_id uuid REFERENCES tenants(id),
 claimed_at timestamptz,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE billing_browser_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 session_hash text NOT NULL UNIQUE CHECK (length(session_hash)=64),
 claim_id uuid REFERENCES checkout_claims(id),
 tenant_id uuid REFERENCES tenants(id),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
 revoked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK (claim_id IS NOT NULL OR tenant_id IS NOT NULL)
);
CREATE TABLE billing_recovery_codes (
 tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
 code_hash text NOT NULL UNIQUE CHECK(length(code_hash)=64),
 created_at timestamptz NOT NULL DEFAULT now()
);
-- Store event IDs/types only. Raw Stripe payload can contain personal details.
CREATE TABLE stripe_webhook_events (
 event_id text PRIMARY KEY,
 event_type text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(),
 processed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX billing_browser_sessions_expiry ON billing_browser_sessions(expires_at);
CREATE INDEX checkout_claims_expiry ON checkout_claims(expires_at);
ALTER TABLE checkout_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE checkout_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_browser_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_browser_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_recovery_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_recovery_codes FORCE ROW LEVEL SECURITY;
ALTER TABLE stripe_webhook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE stripe_webhook_events FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_claims_control ON checkout_claims TO hivemind_billing USING (true) WITH CHECK (true);
CREATE POLICY billing_sessions_control ON billing_browser_sessions TO hivemind_billing USING (true) WITH CHECK (true);
CREATE POLICY billing_recovery_control ON billing_recovery_codes TO hivemind_billing USING (true) WITH CHECK (true);
CREATE POLICY billing_events_control ON stripe_webhook_events TO hivemind_billing USING (true) WITH CHECK (true);
GRANT SELECT, INSERT, UPDATE, DELETE ON checkout_claims,billing_browser_sessions,billing_recovery_codes,stripe_webhook_events TO hivemind_billing;
CREATE TABLE billing_admission (
 bucket text PRIMARY KEY,
 window_start timestamptz NOT NULL,
 attempts integer NOT NULL CHECK(attempts>0)
);
CREATE INDEX billing_admission_window ON billing_admission(window_start);
ALTER TABLE billing_admission ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_admission FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_admission_control ON billing_admission TO hivemind_billing USING(true) WITH CHECK(true);
GRANT SELECT,INSERT,UPDATE,DELETE ON billing_admission TO hivemind_billing;
RESET ROLE;
