"""The player-facing public API: launch config, form, receipts, CTA and feedback events."""

import asyncio
import json
import uuid

import asyncpg
import pytest
from api_helpers import INTERNAL, bearer


async def test_public_launch_config(client):
    r = await client.get("/v1/public/launch-configs/acme-demo")
    assert r.status_code == 200
    lc = r.json()
    assert lc["agent_name"] == "Ava" and lc["product_name"] == "acme CRM"
    assert lc["ctas"] == [
        {"id": "book-0", "kind": "book", "label": "Book a call", "url": "https://cal.example/x"}
    ]
    assert lc["form"] is None and lc["embed_origins"] == []

    # Draft and testing configs are hidden unless the player asks for a test session.
    assert (await client.get("/v1/public/launch-configs/initech-demo")).status_code == 404
    assert (await client.get("/v1/public/launch-configs/hooli-demo")).status_code == 404
    hooli = (await client.get("/v1/public/launch-configs/hooli-demo?test=true")).json()
    assert [f["name"] for f in hooli["form"]["fields"]] == ["email", "team"]
    # Only safe branding values are passed to the browser.
    assert hooli["branding"] == {"accent": "#1a7f5a"}
    assert hooli["embed_origins"] == ["https://www.hooli.example"]


async def test_form_is_validated(client, dsn):
    base = {"slug": "hooli-demo", "test": True}
    r = await client.post("/v1/public/sessions", json=base)
    assert r.status_code == 422 and r.json()["error"] == "invalid_form"
    r = await client.post("/v1/public/sessions", json={**base, "form": {"email": "nope"}})
    assert r.json()["detail"] == "Work email must be an email address"
    r = await client.post(
        "/v1/public/sessions", json={**base, "form": {"email": "a@b.co", "team": "Legal"}}
    )
    assert r.status_code == 422
    r = await client.post(
        "/v1/public/sessions", json={**base, "form": {"email": "a@b.co", "extra": "x"}}
    )
    assert r.json()["detail"] == "Unknown form fields: extra"
    r = await client.post(
        "/v1/public/sessions",
        json={**base, "form": {"email": " a@b.co ", "team": "Sales"}},
        headers={"origin": "https://www.hooli.example"},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    ctx = (await client.get(f"/v1/internal/sessions/{sid}/context", headers=INTERNAL)).json()
    assert ctx["form"] == {"email": "a@b.co", "team": "Sales"}

    # An embed on a host that is not allowlisted cannot start sessions.
    r = await client.post(
        "/v1/public/sessions",
        json={**base, "form": {"email": "a@b.co"}},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def _started(client, slug="acme-demo"):
    started = (await client.post("/v1/public/sessions", json={"slug": slug})).json()
    r = await client.post(
        f"/v1/internal/sessions/{started['session_id']}/started", headers=INTERNAL
    )
    assert r.status_code == 204, r.text
    return started


async def test_events_need_the_session_receipt(client, dsn, orgs):
    unstarted = (await client.post("/v1/public/sessions", json={"slug": "acme-demo"})).json()
    started = await _started(client)
    sid, receipt = started["session_id"], started["receipt"]
    other = await _started(client)
    url = f"/v1/public/sessions/{sid}/events"
    click = {"type": "cta.clicked", "cta_id": "book-0", "label": "Bought 1000 seats"}

    assert (await client.post(url, json=click)).status_code == 401
    assert (await client.post(url, json=click, headers=bearer(other["receipt"]))).status_code == 401
    # A call that never started cannot report anything.
    r = await client.post(
        f"/v1/public/sessions/{unstarted['session_id']}/events",
        json=click,
        headers=bearer(unstarted["receipt"]),
    )
    assert r.status_code == 409 and r.json()["error"] == "not_started"
    # Only CTAs of the launch config can be clicked.
    r = await client.post(url, json={**click, "cta_id": "nonexistent"}, headers=bearer(receipt))
    assert r.status_code == 422 and r.json()["error"] == "unknown_cta"
    assert (await client.post(url, json=click, headers=bearer(receipt))).status_code == 204
    feedback = {"type": "feedback", "rating": 4, "comment": "Clear"}
    assert (await client.post(url, json=feedback, headers=bearer(receipt))).status_code == 204
    # The same answer again changes nothing; a new rating replaces the first.
    assert (await client.post(url, json=feedback, headers=bearer(receipt))).status_code == 204
    feedback["rating"] = 5
    assert (await client.post(url, json=feedback, headers=bearer(receipt))).status_code == 204
    r = await client.post(url, json={"type": "feedback", "rating": 9}, headers=bearer(receipt))
    assert r.status_code == 422
    r = await client.post(url, json={"type": "nope"}, headers=bearer(receipt))
    assert r.status_code == 422

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT type, payload FROM session_events WHERE session_id = $1 ORDER BY created_at",
            uuid.UUID(sid),
        )
        topics = await conn.fetch(
            "SELECT topic FROM outbox WHERE payload->>'session_id' = $1 ORDER BY created_at", sid
        )
        await conn.execute(
            "UPDATE sessions SET started_at = now() - interval '2 days' WHERE id = $1",
            uuid.UUID(sid),
        )
    finally:
        await conn.close()
    assert [r["type"] for r in rows] == ["cta.clicked", "feedback"]
    # What is stored comes from the launch config, not from the request.
    assert json.loads(rows[0]["payload"]) == {
        "cta_id": "book-0",
        "kind": "book",
        "label": "Book a call",
        "url": "https://cal.example/x",
        "phase": "in_call",
    }
    assert json.loads(rows[1]["payload"]) == {"rating": 5, "comment": "Clear", "edits": 1}
    assert [t["topic"] for t in topics] == [
        "session.created",
        "session.started",
        "cta.clicked",
        "session.feedback",  # rating 4; the repeat wrote nothing
        "session.feedback",  # rating 5
    ]
    # Receipts expire a day after the call started.
    assert (await client.post(url, json=click, headers=bearer(receipt))).status_code == 410


async def test_event_caps_hold_under_concurrency(client, monkeypatch):
    from ultrademo_api.routers import public

    monkeypatch.setattr(public, "MAX_EVENTS_PER_SESSION", 5)
    started = await _started(client)
    url = f"/v1/public/sessions/{started['session_id']}/events"
    auth = bearer(started["receipt"])
    click = {"type": "cta.clicked", "cta_id": "book-0"}
    codes = await asyncio.gather(*(client.post(url, json=click, headers=auth) for _ in range(12)))
    assert sorted(r.status_code for r in codes) == [204] * 5 + [429] * 7
    # Feedback has its own budget: the CTA cap does not block it, and edits are capped.
    for rating in (1, 2, 3, 4, 5, 1):
        r = await client.post(url, json={"type": "feedback", "rating": rating}, headers=auth)
        assert r.status_code == 204, r.text
    r = await client.post(url, json={"type": "feedback", "rating": 2}, headers=auth)
    assert r.status_code == 429


async def test_session_events_are_tenant_isolated(client, dsn, orgs):
    started = await _started(client)
    click = {"type": "cta.clicked", "cta_id": "book-0"}
    r = await client.post(
        f"/v1/public/sessions/{started['session_id']}/events",
        json=click,
        headers=bearer(started["receipt"]),
    )
    assert r.status_code == 204
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            await conn.execute("SELECT set_config('app.org_id', $1, true)", orgs["acme"]["org_id"])
            assert await conn.fetchval("SELECT count(*) FROM session_events") > 0
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1, true)", orgs["globex"]["org_id"]
            )
            assert await conn.fetchval("SELECT count(*) FROM session_events") == 0
            # Nor can one org write rows for another org's session.
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO session_events (org_id, session_id, type, payload)"
                    " VALUES ($1, $2, 'feedback', '{}')",
                    uuid.UUID(orgs["acme"]["org_id"]),
                    uuid.UUID(started["session_id"]),
                )
    finally:
        await conn.close()
