import json

import asyncpg
import jwt
import pytest

from api_helpers import INTERNAL, bearer


async def test_auth_required(client):
    r = await client.get("/v1/client/launch-configs")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "unauthorized" and body["request_id"].startswith("req_")
    r = await client.get("/v1/client/launch-configs", headers=bearer("ud_aaaaaaaaaa_" + "b" * 32))
    assert r.status_code == 401


async def test_scope_enforced(client, dsn, orgs):
    from ultrademo_api.security import generate_api_key

    key = generate_api_key()
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "INSERT INTO api_keys (org_id, name, prefix, key_hash, scopes) VALUES ($1, 'ro', $2, $3, $4)",
        __import__("uuid").UUID(orgs["acme"]["org_id"]), key.prefix, key.key_hash, ["sessions:read"],
    )
    await conn.close()
    r = await client.get("/v1/client/launch-configs", headers=bearer(key.plaintext))
    assert r.status_code == 403
    assert r.json()["error"] == "insufficient_scope"


async def test_launch_configs_are_tenant_scoped(client, orgs):
    r = await client.get("/v1/client/launch-configs", headers=bearer(orgs["acme"]["api_key"]))
    assert r.status_code == 200
    slugs = [i["slug"] for i in r.json()["items"]]
    assert slugs == ["acme-demo"]
    assert r.json()["items"][0]["url"] == "https://ud.example/d/acme-demo"


async def test_context_link_and_demo_alias(client, orgs):
    h = bearer(orgs["acme"]["api_key"])
    r = await client.post(
        "/v1/client/context-links",
        headers=h,
        json={
            "launch_config_slug": "acme-demo",
            "context": {"account": {"plan": "team", "seats": 40}},
            "recipient": {"first_name": "Dana", "company": "Hooli"},
            "sender": {"first_name": "Sam", "calendar_url": "https://cal.example/sam"},
            "brief": {"emphasis": ["approvals"], "questions": ["SSO?"]},
        },
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["url"] == f"https://ud.example/d/acme-demo?t={out['token']}" and len(out["token"]) == 16

    r = await client.post("/v1/client/demos", headers=h, json={"demo_url_slug": "acme-demo", "first_name": "Lee"})
    assert r.status_code == 201

    # Another org's slug is invisible, not forbidden.
    r = await client.post("/v1/client/demos", headers=bearer(orgs["globex"]["api_key"]), json={"demo_url_slug": "acme-demo"})
    assert r.status_code == 404


async def test_context_limits(client, orgs):
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    r = await client.post(
        "/v1/client/context-links",
        headers=bearer(orgs["acme"]["api_key"]),
        json={"launch_config_slug": "acme-demo", "context": deep},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "validation_error"


async def test_unpublished_demo_is_not_startable(client):
    r = await client.post("/v1/public/sessions", json={"slug": "initech-demo"})
    assert r.status_code == 404
    r = await client.post("/v1/public/sessions", json={"slug": "nope-demo"})
    assert r.status_code == 404


async def _start(client, orgs, **extra):
    link = await client.post(
        "/v1/client/context-links",
        headers=bearer(orgs["acme"]["api_key"]),
        json={"launch_config_slug": "acme-demo", "context": {"deal": "renewal"}},
    )
    r = await client.post("/v1/public/sessions", json={"slug": "acme-demo", "token": link.json()["token"], **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def test_session_lifecycle(client, orgs, settings):
    started = await _start(client, orgs, params={"utm": "email"})
    sid = started["session_id"]

    claims = jwt.decode(started["token"], options={"verify_signature": False})
    dispatch = claims["roomConfig"]["agents"][0]
    assert dispatch["agentName"] == settings.agent_name
    assert json.loads(dispatch["metadata"]) == {"session_id": sid}
    assert claims["video"]["canPublishSources"] == ["microphone"]

    r = await client.get(f"/v1/internal/sessions/{sid}/context")
    assert r.status_code == 401
    ctx = (await client.get(f"/v1/internal/sessions/{sid}/context", headers=INTERNAL)).json()
    assert ctx["product"]["allowed_domains"] == ["app.acme.example"]
    assert ctx["context_link"]["context"] == {"deal": "renewal"}
    assert ctx["params"] == {"utm": "email"}

    assert (await client.post(f"/v1/internal/sessions/{sid}/started", headers=INTERNAL)).status_code == 204
    lines = [
        {"seq": 0, "role": "agent", "content": "Hi, I'm Ava.", "started_at": "2026-09-22T10:00:00Z"},
        {"seq": 1, "role": "participant", "content": "Show me approvals.", "started_at": "2026-09-22T10:00:05Z"},
        {"seq": 2, "role": "participant", "content": "And pricing.", "started_at": "2026-09-22T10:00:30Z"},
    ]
    for _ in range(2):  # retried batch must not duplicate
        r = await client.post(f"/v1/internal/sessions/{sid}/transcript", headers=INTERNAL, json=lines)
        assert r.status_code == 204
    r = await client.post(
        f"/v1/internal/sessions/{sid}/actions",
        headers=INTERNAL,
        json=[{"seq": 0, "tool": "operate_click", "args": {"ref": "e4"}, "status": "ok",
               "result_summary": "Opened Approvals", "latency_ms": 180}],
    )
    assert r.status_code == 204
    r = await client.post(
        f"/v1/internal/sessions/{sid}/cost",
        headers=INTERNAL,
        json=[{"item": "llm_in", "qty": 12000, "usd": "0.06", "rate_card_version": "2026-09"},
              {"item": "llm_cache_read", "qty": 300000, "usd": "0.15", "rate_card_version": "2026-09"}],
    )
    assert r.status_code == 204
    r = await client.post(
        f"/v1/internal/sessions/{sid}/ended",
        headers=INTERNAL,
        json={"reason": "agent_ended", "summary": "Interested in approvals.", "participant_turns": 2},
    )
    assert r.status_code == 204

    h = bearer(orgs["acme"]["api_key"])
    detail = (await client.get(f"/v1/client/sessions/{sid}", headers=h)).json()
    assert detail["status"] == "ended" and detail["is_valid"] is True and detail["participant_joined"]
    assert detail["duration_seconds"] is not None and detail["actions"] == 1
    assert detail["cost_usd"] == pytest.approx(0.21)
    assert detail["context"] == {"deal": "renewal"}
    transcript = (await client.get(f"/v1/client/sessions/{sid}/transcript", headers=h)).json()
    assert [m["seq"] for m in transcript] == [0, 1, 2]

    # Another tenant cannot read it.
    r = await client.get(f"/v1/client/sessions/{sid}", headers=bearer(orgs["globex"]["api_key"]))
    assert r.status_code == 404


async def test_session_pagination(client, orgs):
    for _ in range(3):
        await _start(client, orgs)
    h = bearer(orgs["acme"]["api_key"])
    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await client.get("/v1/client/sessions", headers=h, params=params)).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) >= 3
    assert "email" not in json.dumps(page)


async def test_rls_blocks_cross_tenant_reads(dsn, orgs):
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction():
            await conn.execute("SET LOCAL ROLE ultrademo_app")
            assert await conn.fetchval("SELECT count(*) FROM sessions") == 0  # no tenant set
            await conn.execute("SELECT set_config('app.org_id', $1, true)", orgs["globex"]["org_id"])
            assert await conn.fetchval("SELECT count(*) FROM launch_configs") == 1
            assert await conn.fetchval("SELECT count(*) FROM sessions") == 0
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO products (org_id, name, base_url, allowed_domains)"
                    " VALUES ($1, 'x', 'https://x', '{}')",
                    __import__("uuid").UUID(orgs["acme"]["org_id"]),
                )
    finally:
        await conn.close()
