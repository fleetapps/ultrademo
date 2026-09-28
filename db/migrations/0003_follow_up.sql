-- Post-call follow-up: the outbox relay turns outbox rows into deliveries to customer webhooks,
-- Slack and the viewer's inbox (ADR 16).
--
-- Fan-out happens in the same transaction that marks the outbox row published, so every event
-- gets exactly one delivery row per target. Each delivery then retries on its own schedule.

-- A customer's receiver: a signed webhook (Standard Webhooks) or a Slack incoming webhook.
CREATE TABLE endpoints (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('webhook', 'slack')),
  url         text NOT NULL CHECK (url ~ '^https?://'),
  -- Signs webhook requests. Kept readable because the relay needs it; shown to the customer once.
  secret      text,
  -- Topics this endpoint receives; empty means every topic.
  topics      text[] NOT NULL DEFAULT '{}',
  description text NOT NULL DEFAULT '',
  disabled_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'webhook' OR secret IS NOT NULL)
);

-- What the viewer is emailed after a call, per launch config. Off unless `{"email": {"enabled": true}}`.
ALTER TABLE launch_configs ADD COLUMN follow_up jsonb NOT NULL DEFAULT '{}';

CREATE TABLE deliveries (
  id               uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id           uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  outbox_id        uuid NOT NULL REFERENCES outbox(id) ON DELETE CASCADE,
  channel          text NOT NULL CHECK (channel IN ('webhook', 'slack', 'email')),
  endpoint_id      uuid REFERENCES endpoints(id) ON DELETE CASCADE,
  -- The rendered request (webhook body, Slack message, or email), fixed at fan-out so every retry
  -- sends the same thing.
  request          jsonb NOT NULL,
  status           text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'dead')),
  attempts         int NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  last_error       text,
  response_status  int,
  sent_at          timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CHECK ((channel = 'email') = (endpoint_id IS NULL))
);
CREATE UNIQUE INDEX deliveries_once_idx ON deliveries (outbox_id, channel, coalesce(endpoint_id, outbox_id));
CREATE INDEX deliveries_due_idx ON deliveries (next_attempt_at) WHERE status = 'pending';

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['endpoints', 'deliveries']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (org_id = nullif(current_setting(''app.org_id'', true), '''')::uuid) '
      'WITH CHECK (org_id = nullif(current_setting(''app.org_id'', true), '''')::uuid)', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO ultrademo_app', t);
  END LOOP;
END
$$;

-- The relay works one tenant at a time under RLS. These say which tenants have work, and nothing
-- else, so the relay never reads another org's rows outside a tenant transaction.
CREATE FUNCTION orgs_with_unpublished_outbox(p_limit int)
RETURNS TABLE (org_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT org_id FROM outbox WHERE published_at IS NULL GROUP BY org_id
  ORDER BY min(created_at) LIMIT p_limit
$$;

CREATE FUNCTION orgs_with_due_deliveries(p_limit int)
RETURNS TABLE (org_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT org_id FROM deliveries WHERE status = 'pending' AND next_attempt_at <= now()
  GROUP BY org_id ORDER BY min(next_attempt_at) LIMIT p_limit
$$;

REVOKE ALL ON FUNCTION orgs_with_unpublished_outbox(int), orgs_with_due_deliveries(int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION orgs_with_unpublished_outbox(int), orgs_with_due_deliveries(int)
  TO ultrademo_app;
