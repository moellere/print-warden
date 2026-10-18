"""HAClient.notify_actionable: the companion-app payload."""

import httpx

from print_warden.config import HomeAssistant
from print_warden.ha import HAClient


def _client(calls):
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.read()))
        return httpx.Response(200, json=[])

    cfg = HomeAssistant(
        url="http://ha.test",
        token="t",
        verify_tls=True,
        webhook_id="w",
        notify_service="notify.phone",
    )
    ha = HAClient(cfg)
    ha._c = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ha.test"
    )
    return ha


async def test_sticky_prompt_sets_companion_flags():
    calls: list = []
    ha = _client(calls)
    assert await ha.notify_actionable(
        "Start print?", "body", [("A", "Start")], tag="print_warden_x", sticky=True
    )
    path, raw = calls[0]
    assert path == "/api/services/notify/phone"
    data = httpx.Response(200, content=raw).json()["data"]
    assert data["sticky"] is True and data["persistent"] is True
    assert data["tag"] == "print_warden_x"
    assert data["actions"] == [{"action": "A", "title": "Start"}]


async def test_plain_prompt_has_no_sticky_flags():
    calls: list = []
    ha = _client(calls)
    assert await ha.notify_actionable("Bed?", "body", [("B", "Cleared")])
    data = httpx.Response(200, content=calls[0][1]).json()["data"]
    assert "sticky" not in data and "persistent" not in data
