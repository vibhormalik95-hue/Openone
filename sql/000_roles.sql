-- Run once as a PostgreSQL administrator; passwords are assigned by bootstrap.
-- Idempotent and re-runnable. Never grant runtime roles membership in owner/helper
-- roles. No application role is superuser or BYPASSRLS.
--
-- Managed cloud PostgreSQL (Supabase, RDS) hands the operator a CREATEROLE
-- administrator, not a superuser. PostgreSQL requires real membership in a role to
-- SET ROLE to it or to ALTER ... OWNER TO it, so without the grants below files
-- 001-006 fail under such an administrator with
--   ERROR 42501: must be able to SET ROLE "hivemind_auth"
--   ERROR 42501: must be owner of table tenants
-- Granting the administrator membership in these roles is the opposite of the
-- forbidden direction: no runtime role gains any privilege from it, and the
-- administrator already outranks every role here.
DO $$ BEGIN
  IF current_setting('server_version_num')::integer < 170000 THEN
    RAISE EXCEPTION 'PostgreSQL 17 or newer required';
  END IF;
END $$;
DO $$
DECLARE role_name text;
BEGIN
  -- Owner and SECURITY DEFINER helper identities. NOLOGIN: reachable only by an
  -- administrator SET ROLE, or as the definer of a hardened function.
  -- hivemind_ledger_writer and hivemind_audit_writer are also created by 005/006;
  -- declaring them here lets an operator provision every role in one step.
  -- hivemind_readonly carries no table privileges by default: it exists so an
  -- operator can attach reviewed read-only access without inventing a new role.
  FOREACH role_name IN ARRAY ARRAY['hivemind_owner','hivemind_auth','hivemind_jobrunner',
                                   'hivemind_ledger_writer','hivemind_audit_writer',
                                   'hivemind_readonly'] LOOP
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
-- The predicate is 'SET'/'USAGE', not 'MEMBER'. Since PostgreSQL 16 a membership
-- carries separate set_option and inherit_option flags, and plain membership implies
-- neither. Supabase's automatic supabase_admin -> postgres grant is recorded with
-- set_option = false, so pg_has_role(...,'MEMBER') is true for every role below while
-- SET ROLE and ALTER ... OWNER TO are still refused with 'must be able to SET ROLE'.
DO $$
DECLARE administrator text; role_name text;
BEGIN
  FOREACH administrator IN ARRAY ARRAY[current_user,'postgres'] LOOP
    CONTINUE WHEN NOT EXISTS (SELECT FROM pg_roles WHERE rolname=administrator AND NOT rolsuper);
    -- Owner and SECURITY DEFINER identities: the administrator needs SET ROLE *and*
    -- the roles' own privileges, because 002-006 run ALTER ... OWNER TO,
    -- CREATE OR REPLACE FUNCTION and REVOKE ... FROM PUBLIC against objects these
    -- roles own. A superuser administrator passes those checks by skipping them
    -- outright; a managed-cloud administrator passes them only through an inheriting
    -- membership. Without it PostgreSQL does not fail the REVOKE -- it emits
    -- 'WARNING: no privileges could be revoked' and leaves PUBLIC EXECUTE in place on
    -- every SECURITY DEFINER function, which is silent privilege escalation.
    FOREACH role_name IN ARRAY ARRAY['hivemind_owner','hivemind_auth','hivemind_jobrunner',
                                     'hivemind_ledger_writer','hivemind_audit_writer'] LOOP
      IF NOT (pg_has_role(administrator,role_name,'SET') AND pg_has_role(administrator,role_name,'USAGE')) THEN
        EXECUTE format('GRANT %I TO %I WITH INHERIT TRUE, SET TRUE', role_name, administrator);
      END IF;
    END LOOP;
    -- Runtime roles: SET ROLE only, never inherited. Migrations never own or replace
    -- anything as these roles, and an inheriting membership would silently satisfy the
    -- policies written FOR them, masking the cross-tenant and cross-project denial
    -- tests that are supposed to run under the runtime role.
    FOREACH role_name IN ARRAY ARRAY['hivemind_app','hivemind_worker','hivemind_billing',
                                     'hivemind_readonly'] LOOP
      IF NOT pg_has_role(administrator,role_name,'SET') OR pg_has_role(administrator,role_name,'USAGE') THEN
        EXECUTE format('GRANT %I TO %I WITH INHERIT FALSE, SET TRUE', role_name, administrator);
      END IF;
    END LOOP;
  END LOOP;
END $$;
-- ADMIN OPTION is deliberately never requested here: PostgreSQL rejects granting it
-- back to your own grantor, and an administrator entitled to run this file holds it.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO hivemind_owner,hivemind_app,hivemind_worker,hivemind_billing,hivemind_auth,hivemind_jobrunner;
GRANT CREATE ON SCHEMA public TO hivemind_owner;
-- ALTER FUNCTION ... OWNER TO additionally requires the *incoming* owner to hold
-- CREATE on the function's schema. PostgreSQL skips that check entirely when the
-- administrator running it is a superuser, so a self-hosted superuser install never
-- needs this grant and managed PostgreSQL fails 002/004/005/006 without it with
-- 'permission denied for schema public'. These four identities are NOLOGIN and
-- NOINHERIT: they are reachable only by an administrator SET ROLE or as the definer
-- of the pinned-search_path SECURITY DEFINER functions that 002-006 assign to them,
-- none of which create objects.
GRANT CREATE ON SCHEMA public TO hivemind_auth,hivemind_jobrunner,hivemind_ledger_writer,hivemind_audit_writer;
ALTER ROLE hivemind_app SET statement_timeout = '10s';
ALTER ROLE hivemind_app SET lock_timeout = '3s';
ALTER ROLE hivemind_app SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE hivemind_worker SET statement_timeout = '10s';
ALTER ROLE hivemind_worker SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE hivemind_billing SET statement_timeout = '10s';
