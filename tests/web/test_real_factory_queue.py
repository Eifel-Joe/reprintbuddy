"""With the real client factory, the queue page and one engine tick work (merge of B2 stage 2).

Before the merge the real client had no `ping` and no `list_queue`: both raised
`AttributeError`. Here the transport is an `httpx.MockTransport`; no socket is opened.
"""

import sys
from pathlib import Path

import httpx

from conftest import finish_admin_setup, finish_bambuddy_setup, make_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bambuddy"))
from fixtures import load  # noqa: E402


def _bambuddy(seen: list[str]):
    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(f"{request.method} {path}")
        if path.endswith("/auth/me"):
            return httpx.Response(200, json=load("auth_me.json"))
        return httpx.Response(200, json=[])  # queue, printers: nothing in them

    return answer


async def test_queue_page_and_one_poll_with_the_real_factory(db_path):
    seen: list[str] = []
    factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(_bambuddy(seen)))  # noqa: E731
    client = make_client(db_path, http_client_factory=factory)  # create_app's own bambuddy_factory
    finish_bambuddy_setup(finish_admin_setup(client))

    assert client.get("/queue").status_code == 200

    result = await client.app.state.ticker.tick(poll=True)

    assert result.skipped is None
    assert client.app.state.ticker.status.reachable is True
    assert client.app.state.ticker.status.bambuddy_queue_ok is True
    assert any(call.startswith("GET ") and "/queue" in call for call in seen)
