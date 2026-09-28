-- 0003: let the migrating user act as ultrademo_app.
--
-- Every transaction runs `SET LOCAL ROLE ultrademo_app`, which needs membership with the SET
-- option. A superuser (local compose, CI) already has it. On Postgres 16+, a non-superuser with
-- CREATEROLE (managed Postgres such as Render) gets only ADMIN on the role it created in 0001, so
-- SET ROLE fails with "permission denied to set role". Granting the role to itself adds SET.
DO $$
BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    EXECUTE format('GRANT ultrademo_app TO %I', current_user);
  END IF;
END
$$;
