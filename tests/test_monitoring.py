import asyncio
import dataclasses

import httpx
import pytest

from print_warden import config, moonraker
from print_warden import watcher as watcher_mod
from print_warden.events import Event, EventStore
from print_warden.jobs import JobStore
from print_warden.watcher import PrinterWatcher


async def test_subscription_adds_discovered_sensors(monkeypatch):
    objects = [
        "extruder",
        "fan",
        "heater_fan hotend_fan",
        "filament_switch_sensor runout",
        "temperature_sensor mcu",
        "gcode_macro X",
    ]

    def handler(req):
        assert req.url.path == "/printer/objects/list"
        return httpx.Response(200, json={"result": {"objects": objects}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        moonraker.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    c = moonraker.MoonrakerClient("ws://x/websocket", None, "http://x")
    subs = await c._objects()
    for name in (
        "fan",
        "heater_fan hotend_fan",
        "filament_switch_sensor runout",
        "temperature_sensor mcu",
        "print_stats",
    ):
        assert name in subs
    assert "gcode_macro X" not in subs


async def test_subscription_falls_back_without_objects_list(monkeypatch):
    def handler(req):
        return httpx.Response(503)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        moonraker.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    subs = await moonraker.MoonrakerClient(
        "ws://x/websocket", None, "http://x"
    )._objects()
    assert subs == moonraker.SUBSCRIBE


class FakeHA:
    def __init__(self):
        self.alerts, self.prompts, self.cleared = [], [], []

    async def alert(self, printer, severity, title, message, snapshot_url=None):
        self.alerts.append(severity)
        return True

    async def notify_actionable(self, title, message, actions, image=None, tag=None):
        self.prompts.append((title, [a for a, _ in actions], image, tag))
        return True

    async def clear_notification(self, tag):
        self.cleared.append(tag)
        return True

    async def state(self, entity):
        return None

    async def light(self, entity, on):
        return None


@pytest.fixture
def w(tmp_path, monkeypatch):
    async def fetch(url, rotate=0, timeout=8.0, flip_v=False):
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(watcher_mod.cameras, "fetch", fetch)
    p = config.Printer(
        name="p", moonraker="http://127.0.0.1:9", cameras={"nozzle": "http://cam"}
    )
    cfg = config.Config(
        printers={"p": p},
        ha=None,
        obico_url=None,
        obico_webhook_secret=None,
        storage_dir=tmp_path,
        keep_days=14,
        host="h",
        port=1,
        public_url="http://w",
        quiet_hours=None,
    )
    jobs = JobStore(tmp_path / "jobs.db")
    pw = PrinterWatcher(cfg, p, EventStore(tmp_path / "e.db"), FakeHA(), jobs)
    return pw


def _printing_job(jobs):
    j = jobs.create(printer="p", name="part", remote_name="warden/a.gcode")
    for s in ("staged", "printing"):
        j = jobs.transition(j["id"], s)
    return j


async def test_quiet_hours_mute_info_and_warning_but_not_critical(w):
    w.cfg = dataclasses.replace(w.cfg, quiet_hours="00:00-23:59")
    for sev in ("info", "warning", "critical"):
        await w._alert(Event("p", f"k_{sev}", sev, "m", alert=True))
    assert w.ha.alerts == ["critical"]


async def test_cooldown_is_configurable(w):
    w.cfg = dataclasses.replace(w.cfg, alert_cooldown_s=0)
    for _ in range(2):
        await w._alert(Event("p", "k", "warning", "m", alert=True))
    assert w.ha.alerts == ["warning", "warning"]


async def test_first_layer_prompt_is_bound_to_the_job(w):
    job = _printing_job(w.jobs)
    await w._first_layer_prompt(
        Event("p", "first_layer_check", "info", "", {"layer": 2})
    )
    _, actions, image, tag = w.ha.prompts[0]
    assert actions == [
        f"PRINT_WARDEN_FIRSTLAYER_{job['id']}",
        f"PRINT_WARDEN_CANCEL_{job['id']}",
    ]
    assert (
        image
        and image.startswith("http://w/files/snapshots/")
        and tag == f"print_warden_fl_{job['id']}"
    )


async def test_first_layer_prompt_skipped_in_quiet_hours_or_without_a_job(w):
    await w._first_layer_prompt(
        Event("p", "first_layer_check", "info", "", {"layer": 2})
    )
    _printing_job(w.jobs)
    w.cfg = dataclasses.replace(w.cfg, quiet_hours="00:00-23:59")
    await w._first_layer_prompt(
        Event("p", "first_layer_check", "info", "", {"layer": 2})
    )
    assert w.ha.prompts == []


async def test_obico_pause_offers_resume_and_cancel(w):
    job = _printing_job(w.jobs)
    ev = Event(
        "p",
        "obico_failure",
        "critical",
        "Obico detected a failure and paused a.gcode",
        {
            "obico": {"event": {"type": "PrintFailure", "print_paused": True}},
            "img_url": "http://obico/i.jpg",
        },
        alert=True,
    )
    await w.handle(ev)
    await __import__("asyncio").gather(*w._tasks)
    _, actions, image, _ = w.ha.prompts[-1]
    assert actions == [
        f"PRINT_WARDEN_RESUME_{job['id']}",
        f"PRINT_WARDEN_CANCEL_{job['id']}",
    ]
    assert image == "http://obico/i.jpg"


async def test_quiet_entity_mutes_while_in_a_quiet_state(w):
    w.cfg = dataclasses.replace(
        w.cfg, quiet_entity="input_boolean.sleeping", quiet_states=("on",)
    )
    states = {"input_boolean.sleeping": "on"}

    async def state(entity):
        return states.get(entity)

    w.ha.state = state
    await w._alert(Event("p", "k1", "warning", "m", alert=True))
    await w._alert(Event("p", "k2", "critical", "m", alert=True))
    assert w.ha.alerts == ["critical"]
    states["input_boolean.sleeping"] = "off"
    await w._alert(Event("p", "k3", "warning", "m", alert=True))
    del states["input_boolean.sleeping"]
    await w._alert(Event("p", "k4", "warning", "m", alert=True))
    assert w.ha.alerts == ["critical", "warning", "warning"]


def test_quiet_entity_config(tmp_path):
    f = tmp_path / "printers.yaml"
    f.write_text(
        "printers:\n  p:\n    moonraker: http://p\n"
        "alerts:\n  quiet_entity: person.me\n  quiet_states: [not_home]\n"
    )
    cfg = config.load(f)
    assert cfg.quiet_entity == "person.me"
    assert cfg.quiet_states == ("not_home",)


async def test_outcome_prompt_after_a_print(w):
    job = _printing_job(w.jobs)
    await w._outcome_prompt(job, Event("p", "print_complete", "info", ""), "nozzle")
    _, actions, image, tag = w.ha.prompts[-1]
    assert actions == [
        f"PRINT_WARDEN_OUTCOME_{job['id']}:good",
        f"PRINT_WARDEN_OUTCOME_{job['id']}:failed",
    ]
    assert image and image.startswith("http://w/files/snapshots/")
    assert tag == f"print_warden_outcome_{job['id']}"
    await w._outcome_prompt(job, Event("p", "print_cancelled", "warning", ""), None)
    _, actions, image, _ = w.ha.prompts[-1]
    assert actions[0].endswith(":adhesion")
    assert image is None


def test_footprint_verdict_is_recorded_on_the_job(w, tmp_path):
    import json

    from print_warden.bedcheck import Check
    from tests.test_footprint import BED, CORNERS, _frame, _square_gcode

    w.p = dataclasses.replace(w.p, bed_check=config.BedCheck(camera="nozzle"))
    w.reference_path.parent.mkdir(parents=True, exist_ok=True)
    w.reference_path.write_bytes(_frame())
    gcode = tmp_path / "j.gcode"
    gcode.write_text(_square_gcode(100, 100, 140, 140))
    job = _printing_job(w.jobs)
    job = w.jobs.note(job["id"], "test", gcode=str(gcode))
    snap = tmp_path / "snapshots" / "after.jpg"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_bytes(_frame())
    check = Check(0.0, "occupied", 0.0, True, "snapshots/after.jpg")
    assert w.footprint_verdict(job, check) is None
    w.jobs.kv_set("bed_calibration:p", json.dumps({"corners": CORNERS, "bed": BED}))
    verdict = w.footprint_verdict(job, check)
    assert verdict.hint.startswith("probably failed")
    assert w.jobs.get(job["id"])["outcome_hint"] == verdict.hint


async def test_a_finished_tower_asks_for_the_best_section(w):
    from print_warden import calibration

    job = _printing_job(w.jobs)
    job = w.jobs.note(
        job["id"],
        "t",
        calibration=calibration.spec("temp", calibration.values("temp", 200), 3),
    )
    await w._outcome_prompt(job, Event("p", "print_complete", "info", ""), None)
    title, actions, _, _ = w.ha.prompts[-1]
    assert title.startswith("Which section of the temp tower")
    assert actions[0] == f"PRINT_WARDEN_CALIB_{job['id']}:1"


def _paused(w, job, state="paused", klippy="ready", targets=(0, 0)):
    w.mr.connected = True
    w.mr.state = {
        "print_stats": {"state": state, "filename": job["remote_name"] if job else ""},
        "webhooks": {"state": klippy},
        "extruder": {"target": targets[0]},
        "heater_bed": {"target": targets[1]},
    }


@pytest.fixture
def mr_calls(monkeypatch):
    calls = []

    async def cancel(base):
        calls.append("cancel")

    async def heaters_off(base):
        calls.append("heaters_off")

    monkeypatch.setattr(watcher_mod.moonraker, "cancel_print", cancel)
    monkeypatch.setattr(watcher_mod.moonraker, "heaters_off", heaters_off)
    monkeypatch.setattr(watcher_mod, "PAUSE_POLL_S", 0.01)
    monkeypatch.setattr(watcher_mod, "COOL_GRACE_S", 0.01)
    return calls


async def _settle(w):
    for _ in range(100):
        if not w._tasks:
            return
        await asyncio.sleep(0.01)


async def test_a_paused_print_nobody_answers_is_cancelled_heaters_off(w, mr_calls):
    w.cfg = dataclasses.replace(w.cfg, paused_cancel_minutes=0.001)
    job = _printing_job(w.jobs)
    _paused(w, job)
    w._watch_pause()
    await _settle(w)
    assert mr_calls == ["cancel"]
    assert w.ha.alerts == ["warning"]
    assert f"print_warden_pause_{job['id']}" in w.ha.cleared
    assert any("cancelled after" in e["note"] for e in w.jobs.get(job["id"])["log"])
    assert w.jobs.kv_get(f"paused_since:{job['id']}") is None


async def test_a_resume_ends_the_watch(w, mr_calls):
    w.cfg = dataclasses.replace(w.cfg, paused_cancel_minutes=0.05)
    job = _printing_job(w.jobs)
    _paused(w, job)
    w._watch_pause()
    await asyncio.sleep(0.03)
    _paused(w, job, state="printing")
    await _settle(w)
    assert mr_calls == []
    assert w.jobs.kv_get(f"paused_since:{job['id']}") is None


async def test_the_pause_start_survives_a_restart_and_zero_turns_it_off(w, mr_calls):
    import time

    job = _printing_job(w.jobs)
    _paused(w, job)
    w.cfg = dataclasses.replace(w.cfg, paused_cancel_minutes=0)
    w._watch_pause()
    await _settle(w)
    assert mr_calls == [] and not w._tasks
    w.cfg = dataclasses.replace(w.cfg, paused_cancel_minutes=30)
    w.jobs.kv_set(f"paused_since:{job['id']}", str(time.time() - 31 * 60))
    w._watch_pause()
    await _settle(w)
    assert mr_calls == ["cancel"]


async def test_heaters_still_on_after_a_failed_print_are_switched_off(w, mr_calls):
    job = _printing_job(w.jobs)
    _paused(w, job, state="error", targets=(210, 60))
    await w._cool_after_failure("print_error")
    assert mr_calls == ["heaters_off"]
    assert w.ha.alerts == ["warning"]
    for state, klippy, targets in (
        ("error", "ready", (0, 0)),  # already cold
        ("printing", "ready", (210, 60)),  # a new print started
        (
            "error",
            "shutdown",
            (210, 60),
        ),  # Klipper shut down: heaters are off and gcode would fail
    ):
        _paused(w, job, state=state, klippy=klippy, targets=targets)
        await w._cool_after_failure("print_cancelled")
    assert mr_calls == ["heaters_off"]


async def test_the_pause_alert_says_when_it_cancels(w, mr_calls):
    w.cfg = dataclasses.replace(w.cfg, paused_cancel_minutes=30)
    ev = Event("p", "print_paused", "warning", "Paused a.gcode: runout", alert=True)
    await w.handle(ev)
    assert "cancels itself at" in ev.message and (
        "AM" in ev.message or "PM" in ev.message
    )


async def test_cancel_print_switches_the_heaters_off(monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.url.path, req.url.params.get("script")))
        return httpx.Response(200, json={"result": "ok"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        moonraker.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    await moonraker.cancel_print("http://x")
    assert seen == [
        ("/printer/print/cancel", None),
        ("/printer/gcode/script", "TURN_OFF_HEATERS"),
    ]
