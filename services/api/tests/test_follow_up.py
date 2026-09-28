"""Post-call follow-up: endpoint management, the outbox relay, and what webhooks, Slack and email get."""

import json
import uuid
from typing import Any

import asyncpg
import httpx
import pytest
from api_helpers import INTERNAL, bearer, spec
from ultrademo_api import follow_up, relay
from ultrademo_api.bootstrap import bootstrap
from ultrademo_api.db import Database
from ultrademo_api.senders import Outcome, SmtpMailer, TargetNotAllowed, check_target
from ultrademo_protocol import webhooks

HOOK = "https://hooks.umbrella.example/ud"
SLACK = "https://hooks.slack.com/services/T0/B0/xyz"


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, email: dict[str, Any]) -> Outcome:
        self.sent.append(email)
        return Outcome(ok=True)


class Receiver:
    """Stands in for the internet: records requests and answers with `status`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="ok" if self.status < 300 else "nope")

    def to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]


@pytest.fixture(scope="module")
async def umbrella(dsn):
    """An org of its own, so counts here don't depend on the other test files."""
    return await bootstrap(
        dsn,
        spec(
            "umbrella",
            follow_up={"email": {"enabled": True, "message": "Here is what we covered."}},
        ),
    )


@pytest.fixture
async def relay_env(settings):
    db = Database(settings.database_url)
    await db.connect()
    receiver = Receiver()
    mailer = FakeMailer()
    s = settings.model_copy(
        update={"allow_private_targets": True, "email_from": "Demos <demos@ud.example>"}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as http:
        yield relay.Relay(db, s, http, mailer), receiver, mailer
    await db.close()


async def _drain(r: relay.Relay) -> None:
    while await r.publish_once():
        pass
    while await r.deliver_once():
        pass


async def test_endpoint_management(client, umbrella, orgs):
    auth = bearer(umbrella["api_key"])
    url = "/v1/client/webhook-endpoints"
    r = await client.post(url, json={"url": "http://plain.example/x"}, headers=auth)
    assert r.status_code == 422 and r.json()["error"] == "invalid_url"
    r = await client.post(url, json={"kind": "slack", "url": HOOK}, headers=auth)
    assert r.status_code == 422
    r = await client.post(url, json={"url": HOOK, "topics": ["nope"]}, headers=auth)
    assert r.status_code == 422

    r = await client.post(url, json={"url": HOOK, "description": "CRM sync"}, headers=auth)
    assert r.status_code == 201, r.text
    hook = r.json()
    assert hook["secret"].startswith("whsec_") and hook["topics"] == []
    r = await client.post(
        url,
        json={"kind": "slack", "url": SLACK, "topics": ["session.ended", "cta.clicked"]},
        headers=auth,
    )
    assert r.status_code == 201 and r.json()["secret"] is None

    listed = (await client.get(url, headers=auth)).json()["items"]
    assert [e["kind"] for e in listed] == ["webhook", "slack"]
    assert all("secret" not in e for e in listed)
    # Another org sees none of it.
    other = bearer(orgs["globex"]["api_key"])
    assert (await client.get(url, headers=other)).json()["items"] == []
    assert (await client.get(f"{url}/{hook['id']}/deliveries", headers=other)).status_code == 404
    assert (await client.delete(f"{url}/{hook['id']}", headers=other)).status_code == 404


async def _endpoint(client, umbrella, kind: str) -> dict:
    items = (
        await client.get("/v1/client/webhook-endpoints", headers=bearer(umbrella["api_key"]))
    ).json()["items"]
    return next(e for e in items if e["kind"] == kind)


async def _secret(dsn, endpoint_id: str) -> str:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchval(
            "SELECT secret FROM endpoints WHERE id = $1", uuid.UUID(endpoint_id)
        )
    finally:
        await conn.close()


async def test_a_finished_demo_reaches_webhooks_slack_and_the_viewer(
    client, dsn, umbrella, relay_env
):
    r, receiver, mailer = relay_env
    await _drain(r)  # anything earlier tests left behind
    receiver.requests.clear()
    hook = await _endpoint(client, umbrella, "webhook")
    secret = await _secret(dsn, hook["id"])

    link = await client.post(
        "/v1/client/context-links",
        headers=bearer(umbrella["api_key"]),
        json={
            "launch_config_slug": "umbrella-demo",
            "recipient": {
                "first_name": "Jane",
                "last_name": "Doe",
                "email": "jane@globex.example",
                "company": "Globex",
            },
            "sender": {
                "first_name": "Sam",
                "last_name": "Rep",
                "email": "sam@umbrella.example",
                "calendar_url": "https://cal.example/sam",
            },
        },
    )
    assert link.status_code == 201, link.text
    token = link.json()["token"]
    started = (
        await client.post("/v1/public/sessions", json={"slug": "umbrella-demo", "token": token})
    ).json()
    sid = started["session_id"]
    base = f"/v1/internal/sessions/{sid}"
    assert (await client.post(f"{base}/started", headers=INTERNAL)).status_code == 204
    r_click = await client.post(
        f"/v1/public/sessions/{sid}/events",
        json={"type": "cta.clicked", "cta_id": "book-0"},
        headers=bearer(started["receipt"]),
    )
    assert r_click.status_code == 204
    summary = "Jane runs a 12-person team; price sensitive, wants SSO. <b>Hot</b>"
    ended = await client.post(
        f"{base}/ended",
        headers=INTERNAL,
        json={"reason": "agent_ended", "summary": summary, "participant_turns": 4},
    )
    assert ended.status_code == 204

    await _drain(r)

    # Webhooks: every topic, signed, in order, with the outbox id as the event id.
    hooks = receiver.to(HOOK)
    assert [json.loads(q.content)["type"] for q in hooks] == [
        "context_link.created",
        "session.created",
        "session.started",
        "cta.clicked",
        "session.ended",
    ]
    for q in hooks:
        assert webhooks.verify(secret, dict(q.headers), q.content)
        assert not webhooks.verify(secret, dict(q.headers), q.content + b" ")
    last = json.loads(hooks[-1].content)
    assert last["data"] == {"session_id": sid, "reason": "agent_ended", "is_valid": True}
    assert last["org_id"] == umbrella["org_id"]

    # Slack: the click and the end, with the summary for sales, escaped.
    slack = [json.loads(q.content)["text"] for q in receiver.to(SLACK)]
    assert len(slack) == 2
    assert "Jane Doe (Globex)" in slack[0] and "“Book a call” during" in slack[0]
    assert "finished a demo of *umbrella CRM*" in slack[1]
    assert "clicked “Book a call”" in slack[1] and "engaged" in slack[1]
    assert "&lt;b&gt;Hot&lt;/b&gt;" in slack[1] and "<b>" not in slack[1]
    assert f"https://ud.example/app/sessions/{sid}" in slack[1]

    # Email: to the viewer, from the rep's calendar, never the internal summary.
    assert len(mailer.sent) == 1
    email = mailer.sent[0]
    assert email["to"] == "jane@globex.example"
    assert email["reply_to"] == "sam@umbrella.example"
    assert email["subject"] == "Your umbrella CRM demo"
    assert "Hi Jane," in email["text"] and "Here is what we covered." in email["text"]
    assert "Pick a time with Sam: https://cal.example/sam" in email["text"]
    assert f"https://ud.example/d/umbrella-demo?t={token}" in email["text"]
    assert "price sensitive" not in email["text"] + email["html"]

    # Everything was sent once; draining again sends nothing more.
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT channel, status, attempts FROM deliveries WHERE org_id = $1",
            uuid.UUID(umbrella["org_id"]),
        )
        unpublished = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE org_id = $1 AND published_at IS NULL",
            uuid.UUID(umbrella["org_id"]),
        )
    finally:
        await conn.close()
    assert unpublished == 0
    assert sorted(x["channel"] for x in rows) == ["email"] + ["slack"] * 2 + ["webhook"] * 5
    assert {(x["status"], x["attempts"]) for x in rows} == {("sent", 1)}
    n = len(receiver.requests)
    await _drain(r)
    assert len(receiver.requests) == n and len(mailer.sent) == 1

    listed = await client.get(
        f"/v1/client/webhook-endpoints/{hook['id']}/deliveries",
        headers=bearer(umbrella["api_key"]),
    )
    assert [d["type"] for d in listed.json()][0] == "session.ended"
    assert {d["status"] for d in listed.json()} == {"sent"}


async def test_failed_deliveries_back_off_then_die(client, dsn, umbrella, relay_env):
    r, receiver, _ = relay_env
    await _drain(r)
    receiver.requests.clear()
    receiver.status = 503
    await client.post("/v1/public/sessions", json={"slug": "umbrella-demo"})
    await r.publish_once()
    assert await r.deliver_once() >= 1

    conn = await asyncpg.connect(dsn)
    try:
        q = (
            "SELECT id, status, attempts, response_status, last_error,"
            " next_attempt_at - now() AS wait FROM deliveries"
            " WHERE org_id = $1 AND status = 'pending' AND channel = 'webhook'"
        )
        org = uuid.UUID(umbrella["org_id"])
        d = await conn.fetchrow(q, org)
        assert d["attempts"] == 1 and d["response_status"] == 503
        assert d["last_error"].startswith("HTTP 503")
        assert 25 < d["wait"].total_seconds() <= 30
        # Not due yet: nothing is sent.
        n = len(receiver.requests)
        await r.deliver_once()
        assert len(receiver.requests) == n

        # The last attempt fails too: the delivery is dead, not retried forever.
        await conn.execute(
            "UPDATE deliveries SET next_attempt_at = now(), attempts = $2 WHERE id = $1",
            d["id"],
            len(relay.BACKOFF),
        )
        await r.deliver_once()
        assert await conn.fetchval("SELECT status FROM deliveries WHERE id = $1", d["id"]) == "dead"

        # A retry that succeeds is marked sent.
        receiver.status = 200
        await client.post("/v1/public/sessions", json={"slug": "umbrella-demo"})
        await r.publish_once()
        receiver.status = 500
        await r.deliver_once()
        d2 = await conn.fetchrow(q, org)
        await conn.execute("UPDATE deliveries SET next_attempt_at = now() WHERE id = $1", d2["id"])
        receiver.status = 204
        await r.deliver_once()
        row = await conn.fetchrow(
            "SELECT status, attempts, last_error FROM deliveries WHERE id = $1", d2["id"]
        )
        assert tuple(row) == ("sent", 2, None)
    finally:
        await conn.close()
        receiver.status = 200


async def test_disabled_endpoints_stop_receiving(client, dsn, umbrella, relay_env):
    r, receiver, _ = relay_env
    auth = bearer(umbrella["api_key"])
    created = await client.post(
        "/v1/client/webhook-endpoints",
        json={"url": "https://hooks.umbrella.example/tmp", "topics": ["session.created"]},
        headers=auth,
    )
    ep = created.json()
    await _drain(r)
    receiver.requests.clear()
    await client.post("/v1/public/sessions", json={"slug": "umbrella-demo"})
    await r.publish_once()  # a delivery is queued for it...
    assert (
        await client.delete(f"/v1/client/webhook-endpoints/{ep['id']}", headers=auth)
    ).status_code == 204
    await _drain(r)  # ...and dies with it instead of being sent
    assert receiver.to("https://hooks.umbrella.example/tmp") == []
    assert receiver.to(HOOK)  # the other endpoint still got the event
    deliveries = (
        await client.get(f"/v1/client/webhook-endpoints/{ep['id']}/deliveries", headers=auth)
    ).json()
    assert [(d["status"], d["last_error"]) for d in deliveries] == [("dead", "endpoint disabled")]
    listed = (await client.get("/v1/client/webhook-endpoints", headers=auth)).json()["items"]
    assert ep["id"] not in [e["id"] for e in listed]


# -- rendering (no database) ---------------------------------------------------------------------


def _facts(**kw) -> follow_up.SessionFacts:
    base: dict[str, Any] = {
        "session_id": uuid.uuid4(),
        "type": "prod",
        "validity": "valid",
        "participant_joined": True,
        "duration_s": 600,
        "end_reason": "agent_ended",
        "summary": "Wants SSO",
        "org_name": "Acme",
        "product_name": "Acme CRM",
        "agent_name": "Ava",
        "launch_config_slug": "acme-demo",
        "launch_config_name": "Acme demo",
        "ctas": [{"kind": "book", "label": "Book", "url": "https://cal.example/team"}],
        "follow_up": {"email": {"enabled": True}},
        "form": {"email": "lee@corp.example"},
        "recipient": {},
        "sender": {},
        "link_token": None,
    }
    return follow_up.SessionFacts(**{**base, **kw})


def test_email_only_for_engaged_real_viewers(settings):
    s = settings.model_copy(update={"email_from": "demos@ud.example"})
    email = follow_up.follow_up_email(s, _facts())
    assert email is not None and email["to"] == "lee@corp.example"
    # No rep: the launch config's book CTA, signed by the team.
    assert "Pick a time with our team: https://cal.example/team" in email["text"]
    assert email["text"].rstrip().endswith("The Acme team") and "reply_to" not in email
    assert "Wants SSO" not in email["text"]
    assert follow_up.follow_up_email(s, _facts(validity="invalid")) is None
    assert follow_up.follow_up_email(s, _facts(type="test")) is None
    assert follow_up.follow_up_email(s, _facts(follow_up={})) is None
    assert follow_up.follow_up_email(s, _facts(form={})) is None
    assert follow_up.follow_up_email(s, _facts(form={"email": "a@b.co\r\nBcc: x@y.z"})) is None
    assert follow_up.follow_up_email(settings, _facts()) is None  # no sender address configured
    # An http calendar link is not offered.
    email = follow_up.follow_up_email(
        s, _facts(ctas=[{"kind": "book", "url": "http://cal.example"}])
    )
    assert "Pick a time" not in email["text"]
    # The HTML part escapes what the customer and viewer typed.
    email = follow_up.follow_up_email(
        s, _facts(follow_up={"email": {"enabled": True, "message": "<script>x</script>"}})
    )
    assert "<script>" not in email["html"] and "&lt;script&gt;" in email["html"]


def test_slack_skips_test_sessions_and_no_shows(settings):
    assert follow_up.slack_message(settings, "session.ended", _facts(type="test"), {}) is None
    assert (
        follow_up.slack_message(settings, "session.ended", _facts(participant_joined=False), {})
        is None
    )
    assert follow_up.slack_message(settings, "session.created", _facts(), {}) is None
    msg = follow_up.slack_message(
        settings, "session.ended", _facts(end_reason="handoff", duration_s=20), {}
    )
    assert "asked for a person" in msg["text"] and "under a minute" in msg["text"]
    assert "lee@corp.example" in msg["text"]


# -- senders -------------------------------------------------------------------------------------


async def test_targets_must_be_public_https():
    with pytest.raises(TargetNotAllowed, match="https"):
        await check_target("http://example.com/x", allow_private=False)
    for url in (
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://[::1]/x",
        "https://10.0.0.8/",
    ):
        with pytest.raises(TargetNotAllowed, match="non-public"):
            await check_target(url, allow_private=False)
    with pytest.raises(TargetNotAllowed):
        await check_target("ftp://example.com/x", allow_private=True)
    await check_target("http://localhost:9999/x", allow_private=True)


async def test_smtp_message_shape_and_header_injection():
    m = SmtpMailer("smtp://u:p%40ss@mail.example:2525", "Demos <demos@ud.example>")
    assert (m._host, m._port, m._user, m._password) == ("mail.example", 2525, "u", "p@ss")
    msg = m._message(
        {"to": "a@b.co", "subject": "Hi", "text": "plain", "html": "<p>x</p>", "reply_to": "r@b.co"}
    )
    assert msg["To"] == "a@b.co" and msg["Reply-To"] == "r@b.co"
    assert msg["Message-ID"].endswith("@ud.example>")
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]
    out = await m.send({"to": "a@b.co", "subject": "Hi\r\nBcc: evil@x.y", "text": "x"})
    assert not out.ok and out.permanent
    with pytest.raises(ValueError):
        SmtpMailer("https://mail.example", "x@y.z")
