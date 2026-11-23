# ruff: noqa: F811  (pytest fixtures imported from test_server are parameters here)
import dataclasses
import json
import re
import time

import pytest
from starlette.testclient import TestClient

from print_warden import inbox, server, signin
from tests.test_server import _staged, clean_queues, ready  # noqa: F401 (fixtures)

PASSWORD = "op-pass"


class Kv:
    def __init__(self):
        self.d = {}

    def kv_get(self, k, default=None):
        return self.d.get(k, default)

    def kv_set(self, k, v):
        self.d[k] = v

    def kv_delete(self, k):
        return self.d.pop(k, None) is not None

    def kv_prefix(self, prefix):
        return {k: v for k, v in sorted(self.d.items()) if k.startswith(prefix)}


def test_the_cookie_is_bound_to_the_password_and_expires():
    v = signin.cookie_value(PASSWORD, now=1000, days=1)
    assert signin.cookie_ok(v, PASSWORD, now=1000 + 3600)
    assert not signin.cookie_ok(v, PASSWORD, now=1000 + 2 * 86400)
    assert not signin.cookie_ok(v, "another", now=1000)
    assert not signin.cookie_ok(v[:-1] + "0", PASSWORD, now=1000)
    assert not signin.cookie_ok(v, None) and not signin.cookie_ok(None, PASSWORD)
    assert signin.password_ok(PASSWORD, PASSWORD)
    assert not signin.password_ok("", None) and not signin.password_ok("x", PASSWORD)


def test_the_limiter_and_the_next_page():
    lim = signin.Limiter()
    for i in range(signin.MAX_FAILS):
        assert lim.locked(now=100 + i) == 0
        lim.failed(now=100 + i)
    assert lim.locked(now=110) > 0
    assert lim.locked(now=100 + signin.LOCK_S + 1) == 0
    assert signin.safe_next("/status") == "/status"
    for bad in ("https://evil.example", "//evil.example", "/\\evil", None, ""):
        assert signin.safe_next(bad) == "/inbox"


def test_prompts_are_kept_by_tag_until_answered_or_old():
    kv = Kv()
    inbox.record(
        kv,
        "a",
        "Start?",
        "m",
        [("PRINT_WARDEN_START_1", "Start"), ("PRINT_WARDEN_SKIP_1", "Skip")],
        now=10,
    )
    inbox.record(
        kv, "a", "Start?", "newer", [("PRINT_WARDEN_START_1", "Start")], now=20
    )
    inbox.record(kv, "b", "Bed?", "m", [("PRINT_WARDEN_BEDCLEAR_p", "Cleared")], now=30)
    inbox.record(kv, "old", "x", "m", [("PRINT_WARDEN_X_1", "X")], now=0)
    got = inbox.open_prompts(kv, now=inbox.MAX_AGE_S + 5)
    assert [p["tag"] for p in got] == ["b", "a"] and got[1]["message"] == "newer"
    assert "prompt:old" not in kv.d
    assert [p["tag"] for p in inbox.answered(kv, "PRINT_WARDEN_START_1")] == ["a"]
    assert inbox.answered(kv, "PRINT_WARDEN_START_1") == []
    inbox.clear(kv, "b")
    assert inbox.open_prompts(kv) == []


def test_the_page_escapes_and_asks_for_typed_replies():
    page = inbox.render(
        "",
        [{"tag": "t", "title": "<b>", "message": "m", "at": time.time(),
          "actions": [["PRINT_WARDEN_VALIDATE_x:0", "Pass"], ["PRINT_WARDEN_VALIDATE_x:1", "Fail", "textInput"]]}],
        "c",
    )  # fmt: skip
    assert "&lt;b&gt;" in page and "<b>" not in page
    assert 'value="PRINT_WARDEN_VALIDATE_x:0">Pass</button>' in page
    assert '<input name="reply" required' in page


@pytest.fixture
def op(monkeypatch):
    monkeypatch.setattr(
        server, "CFG", dataclasses.replace(server.CFG, operator_password=PASSWORD)
    )
    monkeypatch.setattr(server.asgi, "password", PASSWORD)
    for k in server.JOBS.kv_prefix(inbox.KEY):
        server.JOBS.kv_delete(k)
    server.SIGNIN_LIMIT.reset()
    yield TestClient(server.asgi, follow_redirects=False)
    server.SIGNIN_LIMIT.reset()


def _csrf(page):
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page)[1]


def _sign_in(c):
    form = c.get("/signin?next=/inbox").text
    r = c.post(
        "/signin", data={"csrf": _csrf(form), "next": "/inbox", "password": PASSWORD}
    )
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    return _csrf(c.get("/inbox").text)


def test_the_token_alone_answers_nothing(op, clean_queues, ready):
    j = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    inbox.record(server.JOBS, f"print_warden_{j['id']}", "Start print?", "m",
                 [(f"PRINT_WARDEN_START_{j['id']}", "Start")])  # fmt: skip
    r = op.get("/inbox?token=test-token")
    assert r.status_code == 303 and r.headers["location"] == "/signin?next=/inbox"
    csrf = server.stage.csrf_token(server.CFG.api_token)
    r = op.post("/inbox?token=test-token", data={"csrf": csrf, "action": f"PRINT_WARDEN_START_{j['id']}"},
                headers={"authorization": "Bearer test-token"})  # fmt: skip
    assert r.status_code == 403
    assert server.JOBS.get(j["id"])["state"] == "awaiting_approval" and ready == []


def test_a_start_from_the_inbox_is_a_tap(op, clean_queues, ready):
    j = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    start = f"PRINT_WARDEN_START_{j['id']}"
    inbox.record(server.JOBS, f"print_warden_{j['id']}", "Start print?", "part on testp",
                 [(start, "Start"), (f"PRINT_WARDEN_SKIP_{j['id']}", "Skip")])  # fmt: skip
    csrf = _sign_in(op)
    page = op.get("/inbox").text
    assert f'value="{start}">Start</button>' in page
    assert "inbox (1)" in op.get("/status").text
    r = op.post("/inbox", data={"csrf": csrf, "action": start})
    assert r.status_code == 200 and "Done." in r.text
    job = server.JOBS.get(j["id"])
    assert job["authorized_by"] == "the web inbox" and ready == ["warden/p.gcode"]
    assert "Nothing is waiting" in op.get("/inbox").text
    again = op.post("/inbox", data={"csrf": csrf, "action": start})
    assert again.status_code == 409 and "already answered" in again.text
    assert len(ready) == 1


def test_an_expired_or_lost_offer_can_be_sent_again(
    op, clean_queues, ready, monkeypatch
):
    old = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time() - 7200
    )
    inbox.record(server.JOBS, f"print_warden_{old['id']}", "Start print?", "m",
                 [(f"PRINT_WARDEN_START_{old['id']}", "Start")])  # fmt: skip
    lost = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    csrf = _sign_in(op)
    page = op.get("/inbox").text
    assert (
        f"PRINT_WARDEN_START_{old['id']}" not in page and "This offer expired" in page
    )
    assert f'value="PRINT_WARDEN_REOFFER_{lost["id"]}">Offer again' in page
    assert (
        op.post(
            "/inbox", data={"csrf": csrf, "action": f"PRINT_WARDEN_START_{old['id']}"}
        ).status_code
        == 409
    )
    offered = []

    async def request_start(job_id):
        offered.append(job_id)
        return {}

    monkeypatch.setattr(server, "request_start", request_start)
    r = op.post(
        "/inbox", data={"csrf": csrf, "action": f"PRINT_WARDEN_REOFFER_{old['id']}"}
    )
    assert r.status_code == 200 and offered == [old["id"]] and ready == []


def test_a_typed_reply_and_the_prompt_hooks(op):
    async def go():
        await server.HA.notify_actionable(
            "Row 4?", "Start by tap",
            [("PRINT_WARDEN_VALIDATE_s1:0", "Pass"), ("PRINT_WARDEN_VALIDATE_s1:1", "Fail", "textInput")],
            tag="print_warden_validate_s1",
        )  # fmt: skip

    import asyncio

    asyncio.run(go())
    server.JOBS.kv_set("operator_prompt:s1", json.dumps({"choices": ["Pass", "Fail"]}))
    csrf = _sign_in(op)
    assert "Row 4?" in op.get("/inbox").text
    r = op.post(
        "/inbox",
        data={
            "csrf": csrf,
            "action": "PRINT_WARDEN_VALIDATE_s1:1",
            "reply": "no tap arrived",
        },
    )
    assert r.status_code == 200
    got = json.loads(server.JOBS.kv_get("operator_answer:s1"))
    assert (got["choice"], got["note"]) == ("Fail", "no tap arrived")
    assert server.JOBS.kv_prefix(inbox.KEY) == {}
    asyncio.run(go())
    asyncio.run(server.HA.clear_notification("print_warden_validate_s1"))
    assert server.JOBS.kv_prefix(inbox.KEY) == {}


def test_pages_take_the_sign_in_and_send_strangers_to_it(op):
    r = op.get("/status")
    assert r.status_code == 303 and r.headers["location"] == "/signin?next=/status"
    assert op.post("/mcp", json={}).status_code == 401
    form = _csrf(op.get("/signin").text)
    for _ in range(signin.MAX_FAILS):
        assert (
            op.post("/signin", data={"csrf": form, "password": "wrong"}).status_code
            == 401
        )
    assert (
        op.post("/signin", data={"csrf": form, "password": PASSWORD}).status_code == 429
    )
    server.SIGNIN_LIMIT.reset()
    csrf = _sign_in(op)
    assert op.get("/status").status_code == 200 and op.get("/stage").status_code == 200
    assert op.post("/mcp", json={}).status_code == 401
    assert op.post("/signout", data={"csrf": csrf}).status_code == 303
    assert op.get("/status").status_code == 303


def test_sign_in_is_off_without_a_password(client):
    page = client.get("/signin").text
    assert "PRINT_WARDEN_OPERATOR_PASSWORD" in page and 'type="password"' not in page
    assert client.get("/inbox", follow_redirects=False).status_code == 303
    assert client.get("/status").status_code == 401


@pytest.fixture
def client():
    return TestClient(server.asgi)


def test_a_prompt_whose_job_moved_on_is_withdrawn(op, clean_queues, monkeypatch):
    j = server.JOBS.transition(_staged()["id"], "printing")
    tag = f"print_warden_fl_{j['id']}"
    inbox.record(server.JOBS, tag, "First layers", "m",
                 [(f"PRINT_WARDEN_FIRSTLAYER_{j['id']}", "Looks good"), (f"PRINT_WARDEN_CANCEL_{j['id']}", "Cancel")])  # fmt: skip
    withdrawn = []

    async def clear(t):
        withdrawn.append(t)
        return True

    monkeypatch.setattr(server.HA, "clear_notification", clear)
    _sign_in(op)
    assert "First layers" in op.get("/inbox").text
    server.JOBS.transition(j["id"], "done")
    assert "Nothing is waiting" in op.get("/inbox").text
    assert withdrawn == [tag] and server.JOBS.kv_prefix(inbox.KEY) == {}


async def test_a_notification_body_opens_the_inbox(monkeypatch):
    from print_warden.config import HomeAssistant
    from print_warden.ha import HAClient

    ha = HAClient(
        HomeAssistant(
            url="http://ha", token=None, webhook_id="w", notify_service="notify.phone"
        )
    )
    posted = []

    async def post(url, json):
        posted.append(json)
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(ha._c, "post", post)
    await ha.notify_actionable("t", "m", [("A", "a")], image="http://img")
    assert posted[-1]["data"]["clickAction"] == "http://img"
    ha.inbox_url = "http://warden/inbox"
    await ha.notify_actionable("t", "m", [("A", "a")], image="http://img")
    assert posted[-1]["data"]["clickAction"] == "http://warden/inbox"
    await ha.notify_actionable("t", "m", [("A", "a")], click_url="http://x")
    assert posted[-1]["data"]["clickAction"] == "http://x"


def _tap(action, via="phone tap"):
    import asyncio

    return asyncio.run(server._on_ha_action(action, {}, via))


def _undo_id(page, choice):
    m = re.search(
        rf"<b>{choice}</b>.*?name=\"undo\" value=\"([0-9.]+)\"", page, re.DOTALL
    )
    return m[1] if m else None


def test_a_mistaken_skip_is_undone(op, clean_queues, ready):
    j = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    inbox.record(server.JOBS, f"print_warden_{j['id']}", "Start print?", "m",
                 [(f"PRINT_WARDEN_START_{j['id']}", "Start"), (f"PRINT_WARDEN_SKIP_{j['id']}", "Skip")])  # fmt: skip
    _tap(f"PRINT_WARDEN_SKIP_{j['id']}")
    assert server.JOBS.get(j["id"])["state"] == "skipped"
    csrf = _sign_in(op)
    page = op.get("/inbox").text
    assert "Recent" in page and "phone tap" in page
    uid = _undo_id(page, "Skip")
    r = op.post("/inbox", data={"csrf": csrf, "undo": uid})
    assert r.status_code == 200 and "Undone: Skip." in r.text
    job = server.JOBS.get(j["id"])
    assert job["state"] in ("staged", "awaiting_approval")
    assert any("undo: Skip reversed (the web inbox)" in e["note"] for e in job["log"])
    assert op.post("/inbox", data={"csrf": csrf, "undo": uid}).status_code == 409
    assert "undone" in op.get("/inbox").text


def test_a_label_is_undone_and_asked_again(op, clean_queues):
    j = server.JOBS.transition(
        server.JOBS.transition(_staged()["id"], "printing")["id"], "done"
    )
    _tap(f"PRINT_WARDEN_OUTCOME_{j['id']}:good")
    assert server.JOBS.get(j["id"])["outcome"] == "good"
    csrf = _sign_in(op)
    uid = _undo_id(op.get("/inbox").text, "Good")
    assert op.post("/inbox", data={"csrf": csrf, "undo": uid}).status_code == 200
    job = server.JOBS.get(j["id"])
    assert job["outcome"] is None and job["outcome_by"] is None
    assert f'value="PRINT_WARDEN_OUTCOME_{j["id"]}:good">Good' in op.get("/inbox").text


def test_bed_cleared_is_undone_and_its_offer_withdrawn(
    op, clean_queues, ready, monkeypatch
):
    from tests.test_server import _queued

    async def post(url, json):
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(
        server.HA,
        "cfg",
        dataclasses.replace(server.HA.cfg, notify_service="notify.phone"),
    )
    monkeypatch.setattr(server.HA._c, "post", post)
    server.BED.set_state("testp", "occupied", "test")
    q = _queued(est_seconds=600)
    _tap("PRINT_WARDEN_BEDCLEAR_testp")
    assert server.BED.state("testp")["state"] == "clear"
    assert server.JOBS.get(q["id"])["state"] == "awaiting_approval"
    csrf = _sign_in(op)
    uid = _undo_id(op.get("/inbox").text, "Bed cleared")
    assert op.post("/inbox", data={"csrf": csrf, "undo": uid}).status_code == 200
    assert server.BED.state("testp")["state"] == "occupied"
    assert server.JOBS.get(q["id"])["state"] == "staged"
    page = op.get("/inbox").text
    assert (
        "Clear the bed on testp" in page and f"PRINT_WARDEN_START_{q['id']}" not in page
    )


def test_a_start_cannot_be_undone(op, clean_queues, ready):
    j = server.JOBS.transition(
        _staged()["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    _tap(f"PRINT_WARDEN_START_{j['id']}")
    _sign_in(op)
    page = op.get("/inbox").text
    assert "can&#x27;t be undone" in page and 'name="undo"' not in page
