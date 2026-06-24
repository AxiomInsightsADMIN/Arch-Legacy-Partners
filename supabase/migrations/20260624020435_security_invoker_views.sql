BEGIN;
-- Access views respect RLS; public roles lose read access.
-- service_role (dashboard) and postgres (pipeline) bypass RLS and are unaffected.
DO $$
DECLARE vw text;
BEGIN
  FOR vw IN
    SELECT format('%I.%I', n.nspname, c.relname)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname='public' AND c.relkind='v' AND c.relname LIKE 'v\_%' ESCAPE '\'
  LOOP
    EXECUTE 'ALTER VIEW '||vw||' SET (security_invoker = on)';
    EXECUTE 'REVOKE SELECT ON '||vw||' FROM anon, authenticated';
  END LOOP;
END$$;
COMMIT;
