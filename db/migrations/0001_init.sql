-- 0001: core tenancy, demos, sessions (docs/03). Target: Postgres 18; runs on 16+ for local tests.
--
-- Tenancy: every tenant table has org_id and row-level security. The app never queries as the
-- table owner: each transaction runs `SET LOCAL ROLE ultrademo_app` and
-- `set_config('app.org_id', <org>, true)`, so a missing or wrong org id returns no rows instead of
-- another customer's data. Lookups that must happen before the org is known (API key, public demo
-- slug, context-link token) go through the SECURITY DEFINER functions at the end of this file.

-- Postgres 18 ships uuidv7() (time-ordered ids, better index locality than v4). On older servers,
-- define a compatible fallback so local tests and CI run on whatever Postgres is available.
DO $$
BEGIN
  IF current_setting('server_version_num')::int < 180000 THEN
    CREATE OR REPLACE FUNCTION public.uuidv7() RETURNS uuid
    LANGUAGE sql VOLATILE AS $f$
      SELECT encode(
        set_bit(set_bit(
          overlay(uuid_send(gen_random_uuid())
                  placing substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
                  FROM 1 FOR 6),
          52, 1), 53, 1),
        'hex')::uuid
    $f$;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ultrademo_app') THEN
    CREATE ROLE ultrademo_app NOLOGIN;
  END IF;
END
$$;

CREATE TABLE organizations (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  slug        text NOT NULL UNIQUE,
  name        text NOT NULL,
  region      text NOT NULL DEFAULT 'us' CHECK (region IN ('us', 'eu')),
  plan        text NOT NULL DEFAULT 'trial',
  settings    jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name          text NOT NULL,
  prefix        text NOT NULL UNIQUE,
  key_hash      text NOT NULL,
  scopes        text[] NOT NULL DEFAULT '{}',
  expires_at    timestamptz,
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name             text NOT NULL,
  base_url         text NOT NULL,
  allowed_domains  text[] NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE agents (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  product_id  uuid NOT NULL REFERENCES products(id),
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- Immutable: a change is a new version, so a session always knows exactly what it ran.
CREATE TABLE agent_versions (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id         uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  agent_id       uuid NOT NULL REFERENCES agents(id),
  version        int NOT NULL,
  system_prompt  text NOT NULL,
  voice          jsonb NOT NULL DEFAULT '{}',
  policy         jsonb NOT NULL DEFAULT '{}',
  model          jsonb NOT NULL DEFAULT '{}',
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agent_id, version)
);

CREATE TABLE launch_configs (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  slug              text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  name              text NOT NULL,
  description       text NOT NULL DEFAULT '',
  status            text NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'testing', 'published', 'archived')),
  agent_version_id  uuid NOT NULL REFERENCES agent_versions(id),
  ctas              jsonb NOT NULL DEFAULT '[]',
  form_schema       jsonb,
  embed_origins     text[] NOT NULL DEFAULT '{}',
  branding          jsonb NOT NULL DEFAULT '{}',
  max_duration_s    int NOT NULL DEFAULT 1200 CHECK (max_duration_s BETWEEN 60 AND 7200),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Immutable: every create is a new link (same rule as Supersonik's demo links).
CREATE TABLE context_links (
  id                uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  launch_config_id  uuid NOT NULL REFERENCES launch_configs(id),
  token             text NOT NULL UNIQUE,
  context           jsonb NOT NULL DEFAULT '{}',
  recipient         jsonb,
  sender            jsonb,
  brief             jsonb,
  source            text NOT NULL DEFAULT 'api' CHECK (source IN ('api', 'mcp', 'slack', 'ui')),
  created_by        text,
  expires_at        timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  id                  uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id              uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  launch_config_id    uuid NOT NULL REFERENCES launch_configs(id),
  agent_version_id    uuid NOT NULL REFERENCES agent_versions(id),
  context_link_id     uuid REFERENCES context_links(id),
  type                text NOT NULL DEFAULT 'prod' CHECK (type IN ('prod', 'test', 'eval')),
  status              text NOT NULL DEFAULT 'created'
                      CHECK (status IN ('created', 'live', 'ended', 'failed')),
  validity            text NOT NULL DEFAULT 'unknown' CHECK (validity IN ('valid', 'invalid', 'unknown')),
  params              jsonb NOT NULL DEFAULT '{}',
  form                jsonb,
  locale              text NOT NULL DEFAULT 'en',
  visitor_id          text,
  livekit_room        text NOT NULL UNIQUE,
  participant_joined  boolean NOT NULL DEFAULT false,
  summary             text,
  end_reason          text,
  started_at          timestamptz,
  ended_at            timestamptz,
  duration_s          int GENERATED ALWAYS AS
                      (CASE WHEN ended_at IS NOT NULL AND started_at IS NOT NULL
                            THEN extract(epoch FROM (ended_at - started_at))::int END) STORED,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_org_created_idx ON sessions (org_id, created_at DESC, id DESC);

CREATE TABLE transcript_messages (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  session_id    uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq           int NOT NULL,
  role          text NOT NULL CHECK (role IN ('agent', 'participant', 'human_rep', 'system')),
  speaker_name  text,
  content       text NOT NULL,
  source        text NOT NULL DEFAULT 'voice' CHECK (source IN ('voice', 'chat')),
  lang          text,
  interrupted   boolean NOT NULL DEFAULT false,
  started_at    timestamptz NOT NULL DEFAULT now(),
  ended_at      timestamptz,
  UNIQUE (session_id, seq)
);

CREATE TABLE agent_actions (
  id              uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  session_id      uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq             int NOT NULL,
  tool            text NOT NULL,
  args            jsonb NOT NULL DEFAULT '{}',
  status          text NOT NULL CHECK (status IN ('ok', 'needs_confirmation', 'blocked', 'error')),
  result_summary  text NOT NULL DEFAULT '',
  policy_class    text NOT NULL DEFAULT 'allowed' CHECK (policy_class IN ('allowed', 'confirm', 'blocked')),
  element         jsonb,
  latency_ms      int NOT NULL DEFAULT 0,
  started_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (session_id, seq)
);

CREATE TABLE cost_ledger (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  session_id         uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  item               text NOT NULL CHECK (item IN (
                       'llm_in', 'llm_out', 'llm_cache_read', 'llm_cache_write',
                       'stt_sec', 'tts_chars', 'sandbox_sec',
                       'livekit_agent_min', 'livekit_participant_min', 'egress_gb')),
  qty                numeric NOT NULL,
  usd                numeric(12, 6) NOT NULL,
  rate_card_version  text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cost_ledger_session_idx ON cost_ledger (session_id);

CREATE TABLE audit_logs (
  id           uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  actor_kind   text NOT NULL CHECK (actor_kind IN ('user', 'api_key', 'mcp_client', 'agent', 'system')),
  actor_id     text,
  action       text NOT NULL,
  target_type  text NOT NULL,
  target_id    text,
  diff         jsonb,
  ip           inet,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Transactional outbox: rows are written in the same transaction as the change they describe and
-- relayed to webhooks / the event bus afterwards, so an event is never lost or sent for a rollback.
CREATE TABLE outbox (
  id            uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  topic         text NOT NULL,
  payload       jsonb NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz
);
CREATE INDEX outbox_unpublished_idx ON outbox (created_at) WHERE published_at IS NULL;

-- Row-level security on every tenant table.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['api_keys', 'products', 'agents', 'agent_versions', 'launch_configs',
                           'context_links', 'sessions', 'transcript_messages', 'agent_actions',
                           'cost_ledger', 'audit_logs', 'outbox']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (org_id = nullif(current_setting(''app.org_id'', true), '''')::uuid) '
      'WITH CHECK (org_id = nullif(current_setting(''app.org_id'', true), '''')::uuid)', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO ultrademo_app', t);
  END LOOP;
END
$$;

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON organizations
  USING (id = nullif(current_setting('app.org_id', true), '')::uuid);
GRANT SELECT, UPDATE ON organizations TO ultrademo_app;

-- Audit rows are append-only for the app.
REVOKE UPDATE, DELETE ON audit_logs FROM ultrademo_app;

-- Pre-tenant lookups. SECURITY DEFINER runs as the table owner, which RLS does not restrict
-- (tables are not FORCE ROW LEVEL SECURITY). Each returns only what the caller needs next.
CREATE FUNCTION auth_api_key(p_prefix text)
RETURNS TABLE (key_id uuid, org_id uuid, key_hash text, scopes text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT id, org_id, key_hash, scopes FROM api_keys
  WHERE prefix = p_prefix AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())
$$;

CREATE FUNCTION resolve_launch_config(p_slug text)
RETURNS TABLE (launch_config_id uuid, org_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT id, org_id, status FROM launch_configs WHERE slug = p_slug
$$;

CREATE FUNCTION resolve_context_link(p_token text)
RETURNS TABLE (context_link_id uuid, org_id uuid, launch_config_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT id, org_id, launch_config_id FROM context_links
  WHERE token = p_token AND (expires_at IS NULL OR expires_at > now())
$$;

CREATE FUNCTION resolve_session(p_session_id uuid)
RETURNS TABLE (session_id uuid, org_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT id, org_id FROM sessions WHERE id = p_session_id
$$;

REVOKE ALL ON FUNCTION auth_api_key(text), resolve_launch_config(text),
  resolve_context_link(text), resolve_session(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_api_key(text), resolve_launch_config(text),
  resolve_context_link(text), resolve_session(uuid) TO ultrademo_app;
