# ADR 16: Post-call follow-up (2026-09-28)

Status: accepted for the follow-up slice (`services/api`: `relay.py`, `follow_up.py`, `senders.py`; `db/migrations/0003_follow_up.sql`). Revisit each one at the point noted.

## 1. One relay turns the outbox into deliveries
`python -m ultrademo_api.relay` is a separate process built from the api image (compose service `relay`). It has two steps, and any number of replicas can run them side by side because every claim uses `FOR UPDATE SKIP LOCKED`:
- **Publish.** For one org at a time, a tenant transaction claims unpublished outbox rows, writes one `deliveries` row per target, and sets `published_at`. The fan-out and the publish commit together, so an event gets exactly one delivery per target, even if the relay dies halfway.
- **Deliver.** Due deliveries are leased for 5 minutes (`attempts + 1`, `next_attempt_at` pushed out), sent outside any transaction, and then marked `sent`, rescheduled, or `dead`. A relay that dies mid-send leaves the lease to expire, and another replica sends again. Delivery is therefore at least once.

The relay never reads across tenants. Two SECURITY DEFINER functions (`orgs_with_unpublished_outbox`, `orgs_with_due_deliveries`) return only org ids, and all other work runs under RLS, the same way as the api.
The rendered request (the webhook body, the Slack message, or the email) is stored on the delivery at fan-out, so every retry sends the same bytes, and a later edit to a launch config does not change a queued email.
Polling runs every second when idle. Revisit with `LISTEN/NOTIFY` if that latency or load matters.

## 2. Webhooks follow the Standard Webhooks spec
Secrets are `whsec_<base64>`, and requests carry `webhook-id`, `webhook-timestamp` and `webhook-signature: v1,<base64 HMAC-SHA256>` over `id.timestamp.body`. Customers can verify with an off-the-shelf library; `ultrademo_protocol.webhooks.verify` is the reference, and it passes the spec's own test vector.
- `webhook-id` is the delivery id, which stays the same across retries, so receivers dedupe on it. The body's `id` is the outbox id, which is the same event at every endpoint.
- The body is `{id, type, created_at, org_id, data}`, where `data` is the outbox payload unchanged. Session events carry ids, not PII: a receiver calls `GET /v1/client/sessions/{id}` for the details, with its own scoped key.
- Endpoints are managed at `/v1/client/webhook-endpoints` (`webhooks:read`, `webhooks:write`). The secret is shown once. Deleting disables the endpoint and kills its pending deliveries, and `GET .../{id}/deliveries` shows recent attempts. Creates and deletes are written to `audit_logs`.
- The secret is stored readable, because the relay has to sign with it. Revisit with envelope encryption (KMS) at deploy.

## 3. Retries: six, over about 31 hours, then dead
Waits are 30 s, 2 min, 10 min, 1 h, 6 h and 24 h. Any non-2xx response or network error is retried. Redirects are not followed. A target that is not allowed, a refused email recipient, or a disabled endpoint is dead at once.
Revisit with auto-disabling of endpoints that keep failing, and a replay endpoint, when customers ask.

## 4. Targets must be public https
Endpoint URLs must be `https://` when they are saved. At send time, the relay resolves the host and refuses any address that is not global (loopback, private, link-local, metadata), so a customer can't point the relay at our own network. Slack endpoints must be `https://hooks.slack.com/...`. `ULTRADEMO_ALLOW_PRIVATE_TARGETS` turns the check off for tests and local receivers, and the api refuses to start with it on in production.
There is a gap between the relay's DNS check and httpx's own lookup, so DNS rebinding is still possible. Revisit with a pinned-IP transport, or an egress proxy, at deploy.

## 5. Slack is an endpoint kind, not a separate integration
A Slack incoming webhook is stored as an endpoint with `kind = 'slack'`, and it gets the same retries and delivery log. It posts on `session.ended` (viewer, length, whether they engaged, a handoff, CTAs clicked, the agent's summary, a link to the session) and on `cta.clicked`. It posts only for `prod` sessions where the viewer joined, so the customer's own test calls don't reach the sales channel. Text from viewers and the model is escaped for Slack mrkdwn.
Revisit with a Slack app (OAuth, interactive buttons) when the Slack context-link source (`context_links.source = 'slack'`) is built.

## 6. The viewer's follow-up email is opt-in, and has no internal notes
A launch config sends it only with `follow_up.email.enabled = true`. It goes out only on `session.ended`, for a `prod` session that counted as valid (ADR 14 §6), and only when the viewer's address is known (the context link's recipient, or an `email` form field). It carries a greeting, the launch config's optional `message`, the rep's `calendar_url` (from the context link's sender, falling back to the launch config's `book` CTA, https only), and the viewer's demo link. `Reply-To` is the rep.
The agent's summary is written for the sales team (`session_end`: "a short summary for the sales team"), so it goes to Slack and webhooks and never into the email.
Email goes out over SMTP (`ULTRADEMO_SMTP_URL`, `ULTRADEMO_EMAIL_FROM`), because every transactional provider offers it and it adds no dependency. Without them, the relay does not queue emails at all. Addresses are checked before they reach a header, and a header with a line break is refused.
Revisit for unsubscribe handling and per-org sending domains when the first customer sends from their own domain.

## 7. Not in this slice
Insights (a model's judgement of validity, intent and objections) is the last item of the README's "post-call value" and is left for its own slice. It can be a consumer of the same `session.ended` outbox rows.
