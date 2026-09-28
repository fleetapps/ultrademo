import functools
import http.server
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright

SITE = Path(__file__).parent / "site"
CHROMIUM = os.environ.get("ULTRADEMO_OPERATOR_CHROMIUM_EXECUTABLE") or None


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass

    def do_GET(self):  # noqa: N802
        # `?delay=ms` stands in for a slow API.
        delay = parse_qs(urlparse(self.path).query).get("delay")
        if delay:
            time.sleep(int(delay[0]) / 1000)
        super().do_GET()


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
