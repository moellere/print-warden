import asyncio
import time
import uuid

import pytest
from starlette.testclient import TestClient

from print_warden import server, upkeep
from print_warden.events import Event

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
    r = client.post("/upload?token=test-token", files={"file": ("a.obj", b"x")})
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
        server._model_name("evil.obj")
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


@pytest.fixture
def spoolman(monkeypatch):
    spools = {}

    class FakeSpoolman:
        async def spool(self, sid):
            return spools[sid]

    monkeypatch.setattr(server, "SPOOLMAN", FakeSpoolman())
    return spools


async def test_spool_temperatures_become_filament_overrides(spoolman):
    spoolman[7] = {
        "material": "PLA",
        "extruder_temp": 215,
        "bed_temp": 65,
        "remaining_g": 500,
    }
    server.JOBS.kv_set("active_spool:testp", "7")
    try:
        over, note = await server._spool_temps("testp", "PLA")
        assert over["nozzle_temperature"] == ["215"] and over[
            "hot_plate_temp_initial_layer"
        ] == ["65"]
        assert "spool 7" in note
        assert await server._spool_temps("testp", "PETG") == ({}, None)
        spoolman[7] = {"material": "PLA", "extruder_temp": None, "bed_temp": None}
        assert await server._spool_temps("testp", "PLA") == ({}, None)
    finally:
        server.JOBS.kv_set("active_spool:testp", "")


def test_model_history_by_geometry_and_job(clean_queues):
    data = b"solid x\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 1\nendsolid x\n"
    (server.UPLOADS).mkdir(parents=True, exist_ok=True)
    (server.UPLOADS / "bracket.stl").write_bytes(data)
    import hashlib

    h = hashlib.sha256(data).hexdigest()[:16]
    ok = server.JOBS.create(
        printer="testp",
        name="bracket",
        material="PETG",
        quality="strength",
        overrides={"wall_loops": 4},
        model_hash=h,
    )
    for st in ("staged", "printing", "done"):
        server.JOBS.transition(ok["id"], st)
    bad = server.JOBS.create(
        printer="testq", name="renamed", material="PLA", model_hash=h
    )
    for st in ("staged", "printing", "failed"):
        server.JOBS.transition(bad["id"], st)

    hist = server.model_history("bracket.stl")
    assert hist["prints"] == 2 and hist["done"] == 1 and hist["failed"] == 1
    assert hist["last_success"]["overrides"] == {"wall_loops": 4}
    assert hist["last_success"]["quality"] == "strength"
    assert server.model_history(bad["id"])["model_hash"] == h
    with pytest.raises(ValueError):
        server.model_history("nope.stl")


@pytest.fixture
def control(monkeypatch):
    sent = []

    async def cancel(base):
        sent.append("cancel")

    async def resume(base):
        sent.append("resume")

    monkeypatch.setattr(server.moonraker, "cancel_print", cancel)
    monkeypatch.setattr(server.moonraker, "resume_print", resume)
    return sent


def _printing_on(name, file, state="printing"):
    w = _ready_printer(name)
    w.mr.state["print_stats"] = {"state": state, "filename": file}
    j = server.JOBS.create(
        printer=name, name="p", material="PLA", remote_name="warden/p.gcode"
    )
    for st in ("staged", "printing"):
        j = server.JOBS.transition(j["id"], st)
    return j


async def test_cancel_tap_needs_the_same_job_still_printing(clean_queues, control):
    job = _printing_on("testp", "warden/other.gcode")
    res = await server.print_control(job["id"], "cancel", "phone tap")
    assert not res["done"] and "not p" in res["why"] and control == []
    server.WATCHERS["testp"].mr.state["print_stats"]["filename"] = "warden/p.gcode"
    assert (await server.print_control(job["id"], "cancel", "phone tap"))["done"]
    assert control == ["cancel"]


async def test_stale_job_button_cannot_cancel(clean_queues, control):
    old = _printing_on("testp", "warden/p.gcode")
    server.JOBS.transition(old["id"], "done")
    res = await server.print_control(old["id"], "cancel", "phone tap")
    assert not res["done"] and "is done" in res["why"] and control == []


async def test_resume_only_when_paused(clean_queues, control):
    job = _printing_on("testp", "warden/p.gcode", state="printing")
    assert not (await server.print_control(job["id"], "resume", "phone tap"))["done"]
    server.WATCHERS["testp"].mr.state["print_stats"]["state"] = "paused"
    await server._on_ha_action(f"PRINT_WARDEN_RESUME_{job['id']}", {})
    assert control == ["resume"]


async def test_no_mcp_tool_can_cancel_or_resume():
    names = [t.name for t in await server.mcp.list_tools()]
    assert not [n for n in names if "cancel" in n or "resume" in n or "control" in n]


def _finished(printer, est, actual, **kw):
    j = server.JOBS.create(
        printer=printer,
        name="h",
        material="PLA",
        quality="standard",
        est_seconds=est,
        **kw,
    )
    for st in ("staged", "printing", "done"):
        j = server.JOBS.transition(j["id"], st)
    return server.JOBS.note(j["id"], "test", print_duration_s=actual)


def test_corrected_estimates_once_a_printer_has_history(clean_queues):
    server._CORRECTIONS.clear()
    for _ in range(3):
        _finished("testq", 3600, 4320)
    job = server.JOBS.create(printer="testq", name="next", est_seconds=3600)
    pub = server._job_public(job)
    assert (
        pub["est_corrected_seconds"] == 4320
        and pub["est_human"] == "1h 12m (Orca 1h 0m)"
    )
    stats_ = server.printer_stats()
    assert stats_["eta_correction"]["testq"] == 1.2
    assert any(r["printer"] == "testq" for r in stats_["estimate_accuracy"])
    assert server.print_history("testq")[0]["print_duration_s"] == 4320
    assert all(
        r["state"] == "failed" for r in server.print_history("testq", state="failed")
    )
    server._CORRECTIONS.clear()


async def test_entities_publish_changes_only(clean_queues, monkeypatch):
    sent = []

    async def set_state(entity, state, attrs):
        sent.append((entity, state))
        return True

    monkeypatch.setattr(server.HA, "set_state", set_state)
    import dataclasses

    monkeypatch.setattr(
        server,
        "CFG",
        dataclasses.replace(
            server.CFG, ha=dataclasses.replace(server.CFG.ha, token="t")
        ),
    )
    server._PUBLISHED.clear()
    _ready_printer("testp")
    _ready_printer("testq")
    first = await server.publish_entities(force=True)
    assert first == 12 and ("sensor.print_warden_testp_state", "standby") in sent
    assert await server.publish_entities() == 0
    server.WATCHERS["testp"].mr.connected = False
    sent.clear()
    assert await server.publish_entities() == 1
    assert sent == [("sensor.print_warden_testp_state", "offline")]


def test_healthz_contract(client):
    body = client.get("/healthz").json()
    assert body["api"] == server.HEALTH_API == 1 and body["version"]
    p = body["printers"]["testp"]
    assert set(p) == {
        "connected",
        "klippy",
        "state",
        "progress_pct",
        "finishes_in_s",
        "bed",
        "queue",
        "last_update_s_ago",
        "last_shutdown",
        "outcomes_7d",
        "maintenance_due",
    }
    assert "filename" not in str(body)


def test_healthz_reports_an_unrecovered_shutdown_by_cause_only():
    server.STORE.add(
        Event("testq", "klippy_ready", "info", "Klipper ready", alert=False)
    )
    assert server.health_report()["printers"]["testq"]["last_shutdown"] is None
    server.STORE.add(
        Event(
            "testq",
            "klippy_shutdown",
            "critical",
            "Klipper shutdown (thermal runaway): check the heater. secret.gcode",
            {
                "cause": "thermal_runaway",
                "state_message": "Heater extruder not heating at expected rate",
            },
        )
    )
    p = server.health_report()["printers"]["testq"]
    assert p["last_shutdown"]["cause"] == "thermal_runaway"
    assert "thermistor" in p["last_shutdown"]["hint"]
    assert "secret" not in str(p)
    assert "Heater" not in str(p)
    server.STORE.add(
        Event("testq", "klippy_ready", "info", "Klipper ready", alert=False)
    )
    assert server.health_report()["printers"]["testq"]["last_shutdown"] is None


def _labelled_job(state="done", **kw):
    j = server.JOBS.create(
        printer="testp", name="part", material="PLA", quality="draft", **kw
    )
    for s in ("staged", "printing", state):
        j = server.JOBS.transition(j["id"], s)
    return j


async def test_outcome_taps_label_the_job(monkeypatch):
    prompts, cleared = [], []

    async def notify(title, message, actions, image=None, tag=None):
        prompts.append([a for a, _ in actions])
        return True

    async def clear(tag):
        cleared.append(tag)
        return True

    monkeypatch.setattr(server.HA, "notify_actionable", notify)
    monkeypatch.setattr(server.HA, "clear_notification", clear)
    j = _labelled_job()
    await server._on_ha_action(f"PRINT_WARDEN_OUTCOME_{j['id']}:failed", {})
    assert server.JOBS.get(j["id"])["outcome"] == "failed"
    assert prompts[-1][0].endswith(":adhesion")
    await server._on_ha_action(f"PRINT_WARDEN_OUTCOME_{j['id']}:more", {})
    assert prompts[-1][0].endswith(":layer_shift")
    assert server.JOBS.get(j["id"])["outcome_reason"] is None
    await server._on_ha_action(f"PRINT_WARDEN_OUTCOME_{j['id']}:layer_shift", {})
    got = server.JOBS.get(j["id"])
    assert (got["outcome"], got["outcome_reason"], got["outcome_by"]) == (
        "failed",
        "layer_shift",
        "phone tap",
    )
    assert cleared == [f"print_warden_outcome_{j['id']}"]


def test_label_print_tool_and_its_limits():
    j = _labelled_job("failed", error="print_cancelled")
    out = server.label_print(j["id"], "good", "Enoch in chat")
    assert out["outcome"] == "good"
    with pytest.raises(ValueError):
        server.label_print(j["id"], "failed", "x", reason="gremlins")
    running = server.JOBS.create(printer="testp", name="r")
    server.JOBS.transition(running["id"], "staged")
    with pytest.raises(ValueError, match="only finished"):
        server.label_print(running["id"], "good", "x")
    server.JOBS.transition(running["id"], "skipped")


def test_failed_before_warns_only_for_the_same_settings():
    h = "feedc0de" + uuid.uuid4().hex[:8]
    bad = _labelled_job(model_hash=h, overrides={})
    server.label_job(bad["id"], "failed", "adhesion", "test")
    _labelled_job(model_hash=h, overrides={"brim_width": "5"})
    warn = server._failed_before(h, "PLA", "draft", {})
    assert len(warn) == 1
    assert "adhesion" in warn[0]
    assert bad["id"] in warn[0]
    assert server._failed_before(h, "PLA", "draft", {"brim_width": "5"}) == []
    assert server._failed_before(h, "PETG", "draft", {}) == []
    hist = server._history(h)
    assert (hist["done"], hist["failed"]) == (1, 1)


async def test_upkeep_alerts_once_a_day_and_a_tap_resets(monkeypatch):
    import dataclasses

    w = server.WATCHERS["testp"]
    monkeypatch.setattr(
        w,
        "p",
        dataclasses.replace(
            w.p, maintenance=upkeep.parse_maintenance({"belts": {"every_days": 1}})
        ),
    )
    sent, cleared = [], []

    async def notify(title, message, actions, image=None, tag=None):
        sent.append((tag, [a for a, _ in actions]))
        return True

    async def clear(tag):
        cleared.append(tag)
        return True

    monkeypatch.setattr(server.HA, "notify_actionable", notify)
    monkeypatch.setattr(server.HA, "clear_notification", clear)
    monkeypatch.setattr(server, "SPOOLMAN", None)
    now = time.time()
    server.JOBS.kv_set("maint_reset:testp:belts", str(now - 2 * 86400))
    server.JOBS.kv_set("upkeep_alerted:maint:testp:belts", "0")
    assert await server.upkeep_alerts(now) == 1
    tag, actions = sent[-1]
    assert actions == ["PRINT_WARDEN_MAINT_testp:belts"]
    assert await server.upkeep_alerts(now + 3600) == 0
    await server._on_ha_action("PRINT_WARDEN_MAINT_testp:belts", {})
    assert cleared == [tag]
    (belts,) = [m for m in server._maintenance("testp") if m["item"] == "belts"]
    assert not belts["due"]
    rows = {
        e: st
        for _, printer_rows in server._entity_rows().values()
        for e, st, _ in printer_rows
    }
    assert rows["sensor.print_warden_testp_maint_belts"] == "0"
    with pytest.raises(ValueError):
        server.record_maintenance("testp", "flux capacitor", "x")


async def test_submit_split_shares_copies_and_submits_ordinary_jobs(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    stl = tmp_path / "cube.stl"
    stl.write_text(
        "solid c\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 40 0 0\nvertex 0 40 20\n"
        "endloop\nendfacet\nendsolid c\n"
    )

    async def resolve(model):
        return stl

    async def no_spool(printer, mat):
        return {}, None

    sliced = []

    async def slice_stl(cfg, path, out, machine, process, filament, *a, **kw):
        sliced.append(out.name)
        return SimpleNamespace(est_seconds=3600)

    submitted = []

    async def submit(printer, model, material, quality, **kw):
        submitted.append((printer, kw["copies"]))
        return {"printer": printer, "copies": kw["copies"]}

    machine = {
        "printable_area": ["0x0", "200x0", "200x200", "0x200"],
        "printable_height": 200,
    }
    monkeypatch.setattr(server, "_resolve_model", resolve)
    monkeypatch.setattr(server, "_spool_temps", no_spool)
    monkeypatch.setattr(server.slicer, "slice_stl", slice_stl)
    monkeypatch.setattr(server.slicer, "flatten", lambda *a, **k: machine)
    monkeypatch.setattr(server, "submit_job", submit)
    server._CORRECTIONS.clear()
    _ready_printer("testp")
    _ready_printer("testq", material="PETG")
    with pytest.raises(ValueError, match="no printer can take this"):
        await server.submit_split("lab", "cube.stl", "TPU", 4)
    _ready_printer("testq")
    out = await server.submit_split("lab", "cube.stl", "pla", 4)
    assert sorted(submitted) == [("testp", 2), ("testq", 2)]
    plan = out["plan"]
    assert plan["all_done_in_s"] < plan["one_printer_would_take_s"]
    assert sorted(sliced) == ["testp", "testq"]
    with pytest.raises(ValueError, match="do not fit"):
        await server.submit_split("testp,testq", "cube.stl", "PLA", 60)


async def test_cheap_power_holds_long_offers(clean_queues, monkeypatch):
    import dataclasses

    from print_warden import config

    monkeypatch.setattr(
        server,
        "CFG",
        dataclasses.replace(
            server.CFG,
            cheap_power=config.CheapPower("sensor.price", max_price=0.2, min_hours=1),
        ),
    )
    price = {"v": "0.35"}

    async def state(entity):
        return price["v"] if entity == "sensor.price" else None

    offered = []

    async def request(job_id):
        offered.append(job_id)
        server.JOBS.transition(job_id, "awaiting_approval", "test")
        return {"notification_sent": True}

    monkeypatch.setattr(server.HA, "state", state)
    monkeypatch.setattr(server, "request_start", request)
    _ready_printer("testp")
    j = _queued(est_seconds=4 * 3600)
    res = await server.advance_queue("testp")
    assert res["why"].startswith("waiting for cheaper power")
    assert server.JOBS.kv_get("power_hold:testp") == "1"
    price["v"] = "0.1"
    res = await server.advance_queue("testp")
    assert res["action"] == "offered"
    assert offered == [j["id"]]
    assert server.JOBS.kv_get("power_hold:testp") == "0"


def test_calibrate_bed_camera(monkeypatch):
    import json

    machine = {"printable_area": ["0x0", "250x0", "250x210", "0x210"]}
    monkeypatch.setattr(server.slicer, "flatten", lambda *a, **k: machine)
    corners = [[0.2, 0.9], [0.8, 0.9], [0.7, 0.3], [0.3, 0.3]]
    out = server.calibrate_bed_camera("testp", corners, "Enoch in chat")
    assert out["bed_mm"] == (0.0, 0.0, 250.0, 210.0)
    saved = json.loads(server.JOBS.kv_get("bed_calibration:testp"))
    assert saved["corners"] == corners
    with pytest.raises(ValueError):
        server.calibrate_bed_camera(
            "testp", [[0.2, 0.9], [1.5, 0.9], [0.7, 0.3], [0.3, 0.3]], "x"
        )
