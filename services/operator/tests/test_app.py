import os

from fastapi.testclient import TestClient
from ultrademo_operator.app import create_app
from ultrademo_operator.settings import Settings

AUTH = {"authorization": "Bearer t"}
CHROMIUM = os.environ.get("ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE") or None


def test_operator_http_api(site_url):
    settings = Settings(
        internal_token="t", chromium_executable=CHROMIUM, max_sandboxes=1, share_browser=True
    )
    with TestClient(create_app(settings)) as c:
        body = {
            "session_id": "s1",
            "start_url": f"{site_url}/index.html",
            "allowed_domains": ["127.0.0.1"],
        }
        assert c.post("/v1/sandboxes", json=body).status_code == 401
        r = c.post("/v1/sandboxes", json=body, headers=AUTH)
        assert r.status_code == 201, r.text
        sid = r.json()["sandbox_id"]
        assert r.json()["title"] == "Acme CRM · Deals" and r.json()["streaming"] is False

        full = c.post("/v1/sandboxes", json=body, headers=AUTH)
        assert full.status_code == 503 and full.headers["retry-after"] == "2"

        bad = c.post(
            "/v1/sandboxes", json={**body, "start_url": "https://evil.example"}, headers=AUTH
        )
        assert bad.status_code == 503  # capacity is checked before anything is launched

        r = c.post(f"/v1/sandboxes/{sid}/tools/operate_observe", json={"input": {}}, headers=AUTH)
        assert r.status_code == 200 and "Search deals" in r.json()["snapshot"]
        assert c.get("/healthz").json() == {"status": "ok", "sandboxes": 1, "capacity": 1}

        assert c.delete(f"/v1/sandboxes/{sid}", headers=AUTH).status_code == 204
        bad = c.post(
            "/v1/sandboxes", json={**body, "start_url": "https://evil.example"}, headers=AUTH
        )
        assert bad.status_code == 422
        assert c.get("/healthz").json()["sandboxes"] == 0
