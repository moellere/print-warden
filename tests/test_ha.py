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


async def test_persistent_prompt_sets_both_companion_flags():
    calls: list = []
    ha = _client(calls)
    assert await ha.notify_actionable(
        "Start print?", "body", [("A", "Start")], tag="print_warden_x", persistent=True
    )
    path, raw = calls[0]
    assert path == "/api/services/notify/phone"
    data = httpx.Response(200, content=raw).json()["data"]
    assert data["sticky"] is True and data["persistent"] is True
    assert data["tag"] == "print_warden_x"
    assert data["actions"] == [{"action": "A", "title": "Start"}]


async def test_every_prompt_is_sticky_but_swipeable():
    calls: list = []
    ha = _client(calls)
    assert await ha.notify_actionable("Bed?", "body", [("B", "Cleared")])
    data = httpx.Response(200, content=calls[0][1]).json()["data"]
    assert data["sticky"] is True
    assert "persistent" not in data and "clickAction" not in data


async def test_body_tap_opens_the_image_unless_a_url_is_given():
    calls: list = []
    ha = _client(calls)
    img = "http://warden.test/files/snapshots/x.jpg"
    assert await ha.notify_actionable(
        "Layers?", "body", [("L", "Looks good")], image=img
    )
    data = httpx.Response(200, content=calls[0][1]).json()["data"]
    assert data["image"] == img and data["clickAction"] == img
    assert await ha.notify_actionable(
        "Layers?",
        "body",
        [("L", "Looks good")],
        image=img,
        click_url="http://warden.test/status",
    )
    data = httpx.Response(200, content=calls[1][1]).json()["data"]
    assert data["clickAction"] == "http://warden.test/status"
