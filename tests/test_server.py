import asyncio
import time

import pytest
from starlette.testclient import TestClient

from print_warden import server

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client():
    return TestClient(server.asgi)


def test_healthz_is_open(client):
    assert client.get("/healthz").status_code == 200


def test_mcp_and_upload_need_the_token(client):
    assert client.post("/mcp", json={}).status_code == 401
    assert client.post("/upload", files={"file": ("a.stl", b"x")}).status_code == 401
    assert (
        client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_upload_with_token(client):
    r = client.post("/upload", files={"file": ("a.stl", b"solid")}, headers=AUTH)
    assert r.json() == {"ok": True, "model": "a.stl"}
    r = client.post("/upload?token=test-token", files={"file": ("a.3mf", b"x")})
    assert r.status_code == 400


def test_databases_and_models_are_not_served(client):
    (server.CFG.storage_dir / "snapshots" / "s.jpg").write_bytes(b"jpg")
    assert client.get("/files/snapshots/s.jpg").content == b"jpg"
    for path in ("/files/events.db", "/files/jobs.db", "/files/models/a.stl"):
        assert client.get(path).status_code in (401, 404), path
    assert client.get("/files/snapshots/../events.db").status_code == 404


def test_webhook_secrets_fail_closed(client):
    assert client.post("/obico/webhook", json={}).status_code == 403
    assert client.post("/obico/webhook?secret=wrong", json={}).status_code == 403
    assert client.post("/ha/action?secret=obico-secret", json={}).status_code == 403
    assert (
        client.post("/ha/action?secret=ha-secret", json={"action": ""}).status_code
        == 200
    )


def test_unset_secret_disables_route():
    from starlette.requests import Request

    req = Request({"type": "http", "query_string": b"secret=", "headers": []})
    assert not server._secret_ok(req, None)
    assert not server._secret_ok(req, "")


@pytest.fixture
def ready(monkeypatch):
    w = server.WATCHERS["testp"]
    w.mr.connected = True
    w.mr.state = {"webhooks": {"state": "ready"}, "print_stats": {"state": "standby"}}
    server.JOBS.kv_set("loaded_material:testp", "PLA")
    server.JOBS.kv_set("active_spool:testp", "")
    started = []

    async def fake_start(base, remote):
        await asyncio.sleep(0.01)
        started.append(remote)
        return {}

    monkeypatch.setattr(server.moonraker, "start_print", fake_start)
    return started


def _staged(**kw):
    j = server.JOBS.create(
        printer="testp", name="part", material="PLA", remote_name="warden/p.gcode", **kw
    )
    return server.JOBS.transition(j["id"], "staged")


async def test_tap_needs_a_fresh_request(ready):
    j = _staged()
    res = await server._start(j["id"], "phone tap", via_tap=True)
    assert not res["started"] and "not awaiting" in res["blockers"][0]

    server.JOBS.transition(
        j["id"], "awaiting_approval", approval_requested_at=time.time() - 3600
    )
    res = await server._start(j["id"], "phone tap", via_tap=True)
    assert not res["started"] and "expired" in res["blockers"][0]
    assert server.JOBS.get(j["id"])["state"] == "staged"
    assert ready == []

    server.JOBS.transition(
        j["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    res = await server._start(j["id"], "phone tap", via_tap=True)
    assert res["started"] and ready == ["warden/p.gcode"]


async def test_concurrent_starts_send_one_print(ready):
    j = _staged()
    a, b = await asyncio.gather(
        server._start(j["id"], "chat: go"), server._start(j["id"], "chat: go again")
    )
    assert sorted([a["started"], b["started"]]) == [False, True]
    assert ready == ["warden/p.gcode"]
    assert server.JOBS.get(j["id"])["state"] == "printing"


async def test_start_failure_returns_job_to_staged(ready, monkeypatch):
    import httpx

    async def boom(base, remote):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(server.moonraker, "start_print", boom)
    j = _staged()
    with pytest.raises(httpx.ConnectError):
        await server._start(j["id"], "chat: go")
    assert server.JOBS.get(j["id"])["state"] == "staged"


async def test_expire_approvals():
    j = _staged()
    server.JOBS.transition(
        j["id"], "awaiting_approval", approval_requested_at=time.time() - 3600
    )
    fresh = _staged()
    server.JOBS.transition(
        fresh["id"], "awaiting_approval", approval_requested_at=time.time()
    )
    expired = await server.expire_approvals()
    assert j["id"] in expired and fresh["id"] not in expired
    assert server.JOBS.get(j["id"])["state"] == "staged"


def test_model_names():
    assert server._model_name("/x/y/part.STL?dl=1") == "part.STL"
    with pytest.raises(ValueError):
        server._model_name("evil.3mf")
    with pytest.raises(ValueError):
        server._model_name("../../events.db")


@pytest.fixture
def auto_printer(ready, monkeypatch, tmp_path):
    import dataclasses
    import io

    from PIL import Image

    from print_warden import config

    w = server.WATCHERS["testp"]
    orig = w.p
    w.p = dataclasses.replace(
        orig,
        cameras={"bed": "http://cam"},
        auto_start=True,
        auto_start_hours="00:00-23:59",
        bed_check=config.BedCheck(min_agreements=2),
    )
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 120, 120)).save(buf, "JPEG")

    async def fetch(url, rotate=0, timeout=8.0, flip_v=False):
        return buf.getvalue()

    monkeypatch.setattr(server.cameras, "fetch", fetch)
    yield w
    w.p = orig
    if w.reference_path:
        w.reference_path.unlink(missing_ok=True)
    server.BED.set_state("testp", "clear", "test teardown")


async def test_auto_start_needs_a_record_then_starts(auto_printer, ready):
    w = auto_printer
    await w.mark_clear("tap")  # captures the reference
    j = _staged()
    res = await server._start(j["id"], "auto", via_auto=True)
    assert not res["started"] and any("labelled checks" in b for b in res["blockers"])
    assert ready == []

    await w.mark_clear("tap")
    await w.mark_clear("tap")
    res = await server._start(j["id"], "auto", via_auto=True)
    assert res["started"], res["blockers"]
    assert "auto: bed check clear" in server.JOBS.get(j["id"])["authorized_by"]


async def test_occupied_bed_blocks_every_start(ready):
    server.BED.set_state("testp", "occupied", "print_complete")
    try:
        res = await server._start(_staged()["id"], "chat: go")
        assert not res["started"] and "bed not cleared" in res["blockers"][0]
    finally:
        server.BED.set_state("testp", "clear", "test")


async def test_auto_start_off_by_default(ready):
    res = await server._start(_staged()["id"], "auto", via_auto=True)
    assert res["blockers"] == ["auto_start is off for testp"]


def _ready_printer(name, state="standby", material="PLA"):
    w = server.WATCHERS[name]
    w.mr.connected = True
    w.mr.state = {"webhooks": {"state": "ready"}, "print_stats": {"state": state}}
    server.JOBS.kv_set(f"loaded_material:{name}", material)
    return w


@pytest.fixture
def phone(monkeypatch):
    sent = []

    async def notify(title, message, actions, image=None, tag=None):
        sent.append(tag)
        return True

    monkeypatch.setattr(server.HA, "notify_actionable", notify)
    return sent


@pytest.fixture
def clean_queues():
    def wipe():
        for j in server.JOBS.list(None, 500):
            if j["state"] in ("staged", "awaiting_approval", "approved", "sliced"):
                server.JOBS.transition(j["id"], "skipped", "test cleanup")
            elif j["state"] == "printing":
                server.JOBS.transition(j["id"], "failed", "test cleanup")
        for n in ("testp", "testq"):
            server.BED.set_state(n, "clear", "test")

    wipe()
    yield
    wipe()


def _queued(printer="testp", **kw):
    j = server.JOBS.create(
        printer=printer,
        name="q",
        material="PLA",
        remote_name="warden/q.gcode",
        offer=True,
        **kw,
    )
    return server.JOBS.transition(j["id"], "staged")


async def test_queue_offers_the_head_once(clean_queues, ready, phone):
    first, second = _queued(est_seconds=600), _queued(est_seconds=600)
    res = await server.advance_queue("testp")
    assert res["action"] == "offered" and res["job_id"] == first["id"]
    assert server.JOBS.get(first["id"])["state"] == "awaiting_approval"
    res = await server.advance_queue("testp")
    assert res["action"] == "none" and "already waiting" in res["why"]
    assert server.JOBS.get(second["id"])["state"] == "staged"


async def test_queue_waits_for_a_cleared_bed(clean_queues, ready):
    _queued()
    server.BED.set_state("testp", "occupied", "print_complete")
    assert (await server.advance_queue("testp"))["why"] == "bed not cleared"


async def test_blocked_jobs_are_passed_over_and_can_be_waived(
    clean_queues, ready, phone
):
    blocked = _queued(slice_blockers=["nozzle 300°C"])
    ok = _queued()
    res = await server.advance_queue("testp")
    assert res["job_id"] == ok["id"]
    with pytest.raises(ValueError):
        server.waive_blockers(blocked["id"], "no")
    out = server.waive_blockers(blocked["id"], "the operator said: print it anyway")
    assert out["slice_blockers"] == []
    assert "waived" in server.JOBS.get(blocked["id"])["log"][-1]["note"]


def test_move_job_reorders(clean_queues):
    a, b, c = _queued(), _queued(), _queued()
    order = server.move_job(c["id"], 0)
    assert [r["job_id"] for r in order] == [c["id"], a["id"], b["id"]]
    with pytest.raises(ValueError):
        server.move_job(server.JOBS.transition(a["id"], "skipped")["id"], 0)


async def test_location_routes_to_the_printer_free_soonest(clean_queues):
    _ready_printer("testp")
    _ready_printer("testq")
    _queued("testp", est_seconds=7200)
    printer, why = await server._resolve_printer("lab", "PLA")
    assert printer == "testq" and "free in 0 min" in why and "testp 120 min" in why


async def test_location_prefers_loaded_material_over_speed(clean_queues):
    _ready_printer("testp", material="PETG")
    _ready_printer("testq")
    _queued("testq", est_seconds=7200)
    assert (await server._resolve_printer("lab", "PLA"))[0] == "testq"
    with pytest.raises(ValueError, match="has PLA"):
        await server._resolve_printer("lab", "ASA")


async def test_unreachable_printer_is_not_chosen(clean_queues):
    _ready_printer("testp")
    w = _ready_printer("testq")
    w.mr.connected = False
    assert (await server._resolve_printer("lab", "PLA"))[0] == "testp"


async def test_queue_auto_starts_or_falls_back_to_a_tap(
    clean_queues, auto_printer, ready, phone
):
    import dataclasses

    w = auto_printer
    for _ in range(3):
        await w.mark_clear("tap")
    w.p = dataclasses.replace(w.p, finish_window="00:00-00:01")
    late = _queued(est_seconds=3600)
    res = await server.advance_queue("testp")
    assert res["action"] == "offered" and res["job_id"] == late["id"]
    assert any("finish_window" in b for b in res["auto_start_refused"])
    server.JOBS.transition(late["id"], "skipped", "test")

    w.p = dataclasses.replace(w.p, finish_window=None)
    job = _queued(est_seconds=3600)
    res = await server.advance_queue("testp")
    assert res == {"action": "auto_started", "job_id": job["id"]}
    assert ready == ["warden/q.gcode"]


async def test_undelivered_offer_does_not_wedge_the_queue(clean_queues, ready):
    job = _queued()
    res = await server.advance_queue("testp")
    assert res["why"] == "phone notification failed"
    assert server.JOBS.get(job["id"])["state"] == "staged"
