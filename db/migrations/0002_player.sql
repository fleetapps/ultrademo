-- Viewer-side events the demo player reports: CTA clicks (in call and on the post-call screen) and
-- post-call feedback. These are the conversion signals sales acts on, so they are stored per
-- session and also written to the outbox for webhooks (`cta.clicked`, `session.feedback`).

CREATE TABLE session_events (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  session_id  uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  type        text NOT NULL CHECK (type IN ('cta.clicked', 'feedback')),
  payload     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX session_events_session_idx ON session_events (session_id, created_at);
-- One feedback row per session; a second submission replaces the first.
CREATE UNIQUE INDEX session_events_one_feedback_idx ON session_events (session_id)
  WHERE type = 'feedback';

ALTER TABLE session_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON session_events
  USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);
GRANT SELECT, INSERT, UPDATE, DELETE ON session_events TO ultrademo_app;
