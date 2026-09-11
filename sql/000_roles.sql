-- Run once as a PostgreSQL administrator; passwords are assigned by bootstrap.
-- Migration files are immutable after release. Never grant runtime roles membership
-- in owner/helper roles. No application role is superuser or BYPASSRLS.
DO $$
DECLARE role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['hivemind_owner','hivemind_auth','hivemind_jobrunner'] LOOP
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', role_name);
    END IF;
  END LOOP;
  FOREACH role_name IN ARRAY ARRAY['hivemind_app','hivemind_worker','hivemind_billing'] LOOP
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS', role_name);
    END IF;
  END LOOP;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO hivemind_owner,hivemind_app,hivemind_worker,hivemind_billing,hivemind_auth,hivemind_jobrunner;
GRANT CREATE ON SCHEMA public TO hivemind_owner;
ALTER ROLE hivemind_app SET statement_timeout = '10s';
ALTER ROLE hivemind_app SET lock_timeout = '3s';
ALTER ROLE hivemind_app SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE hivemind_worker SET statement_timeout = '10s';
ALTER ROLE hivemind_worker SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE hivemind_billing SET statement_timeout = '10s';
