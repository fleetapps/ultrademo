import functools
import http.server
import os
import threading
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

SITE = Path(__file__).parent / "site"
CHROMIUM = os.environ.get("ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE") or None


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass


@pytest.fixture(scope="session")
def site_url():
    handler = functools.partial(_Quiet, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="session")
async def browser():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(executable_path=CHROMIUM)
        yield pw, b
        await b.close()
