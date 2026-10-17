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
