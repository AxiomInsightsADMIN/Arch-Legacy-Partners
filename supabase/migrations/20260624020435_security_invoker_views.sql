BEGIN;
-- Access views respect RLS; public roles lose read access.
-- service_role (dashboard) and postgres (pipeline) bypass RLS and are unaffected.
DO $$
DECLARE
  vw text;
  rolename text;
BEGIN
  FOR vw IN
    SELECT format('%I.%I', n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname='public' AND c.relkind='v' AND c.relname LIKE 'v\_%' ESCAPE '\'
  LOOP
    EXECUTE 'ALTER VIEW '||vw||' SET (security_invoker = on)';
    -- Revoke is guarded so this migration also applies on environments that
    -- lack the Supabase-managed roles (e.g. the throwaway Postgres in CI).
    -- On Supabase both roles exist, so the effect is identical to an
    -- unconditional REVOKE.
    FOREACH rolename IN ARRAY ARRAY['anon','authenticated'] LOOP
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rolename) THEN
        EXECUTE 'REVOKE SELECT ON '||vw||' FROM '||quote_ident(rolename);
      END IF;
    END LOOP;
  END LOOP;
END$$;
COMMIT;
