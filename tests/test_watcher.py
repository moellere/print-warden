import httpx
import pytest

from print_warden import config
from print_warden.events import Event, EventStore
from print_warden.jobs import JobStore
from print_warden.spoolman import Spoolman
from print_warden.watcher import PrinterWatcher, completion_summary, used_grams

JOB = {"name": "part", "filament_g": 20.0, "filament_m": 6.0, "est_seconds": 3600}


def test_used_grams_scales_estimate():
    assert used_grams(JOB, 3000) == pytest.approx(10.0)
    assert used_grams(JOB, None) is None
    assert used_grams({"filament_g": 5}, 100) is None


def test_completion_summary():
    assert (
        completion_summary(JOB, 3960, 6000)
        == "part done in 1h 06m, est 1h 00m (+10%), 20 g"
    )


def _cfg(tmp_path, track=True):
    p = config.Printer(
        name="p", moonraker="http://127.0.0.1:9", cameras={}, track_spool_usage=track
    )
    return config.Config(
        printers={"p": p},
        ha=None,
        obico_url=None,
        obico_webhook_secret=None,
        storage_dir=tmp_path,
        keep_days=14,
        host="h",
        port=1,
        public_url="http://w",
    ), p


def _spoolman(calls):
    def handler(req):
        calls.append((req.url.path, req.content))
        return httpx.Response(
            200,
            json={"id": 4, "remaining_weight": 900, "filament": {"material": "PLA"}},
        )

    sm = Spoolman("http://spoolman")
    sm._c = httpx.AsyncClient(
        base_url="http://spoolman/api/v1", transport=httpx.MockTransport(handler)
    )
    return sm


async def test_completion_deducts_spool_and_finishes_job(tmp_path):
    cfg, p = _cfg(tmp_path)
    jobs = JobStore(tmp_path / "jobs.db")
    calls = []
    w = PrinterWatcher(
        cfg, p, EventStore(tmp_path / "e.db"), None, jobs, _spoolman(calls)
    )
    j = jobs.create(
        printer="p",
        name="part",
        remote_name="warden/a.gcode",
        filament_g=20,
        filament_m=6,
    )
    for s in ("staged", "approved", "printing"):
        jobs.transition(j["id"], s, spool_id=4) if s == "approved" else jobs.transition(
            j["id"], s
        )
    ev = Event(
        "p",
        "print_complete",
        "info",
        "Complete",
        {
            "filename": "warden/a.gcode",
            "print_duration": 60,
            "filament_used_mm": 3000.0,
        },
        alert=True,
    )
    await w.handle(ev)
    j = jobs.get(j["id"])
    assert j["state"] == "done" and j["filament_used_mm"] == 3000.0
    assert calls == [("/api/v1/spool/4/use", b'{"use_length":3000.0}')]
    assert "900 g left" in j["log"][-1]["note"]
    assert ev.message.startswith("part done in")


async def test_no_deduction_when_tracking_disabled(tmp_path):
    cfg, p = _cfg(tmp_path, track=False)
    jobs = JobStore(tmp_path / "jobs.db")
    calls = []
    w = PrinterWatcher(
        cfg, p, EventStore(tmp_path / "e.db"), None, jobs, _spoolman(calls)
    )
    j = jobs.create(printer="p", name="part", spool_id=4)
    await w.settle_spool(j, 100.0)
    assert not any(path.endswith("/use") for path, _ in calls)
    assert jobs.get(j["id"])["filament_used_mm"] == 100.0


async def test_reconcile_skips_pending_while_a_start_holds_the_lock(tmp_path):
    cfg, p = _cfg(tmp_path)
    jobs = JobStore(tmp_path / "jobs.db")
    w = PrinterWatcher(cfg, p, EventStore(tmp_path / "e.db"), None, jobs)
    j = jobs.create(printer="p", name="part", remote_name="warden/a.gcode")
    jobs.transition(j["id"], "staged")
    jobs.transition(j["id"], "approved")
    async with w.start_lock:
        await w.reconcile_jobs()
    assert jobs.get(j["id"])["state"] == "approved"
    await w.reconcile_jobs()
    assert jobs.get(j["id"])["state"] == "staged"


async def test_outcome_records_duration_grams_and_cost(tmp_path):
    cfg, p = _cfg(tmp_path)
    jobs = JobStore(tmp_path / "jobs.db")

    def handler(req):
        return httpx.Response(
            200,
            json={
                "id": 4,
                "remaining_weight": 900,
                "price": 20.0,
                "initial_weight": 1000,
                "filament": {"material": "PLA"},
            },
        )

    sm = Spoolman("http://spoolman")
    sm._c = httpx.AsyncClient(
        base_url="http://spoolman/api/v1", transport=httpx.MockTransport(handler)
    )
    w = PrinterWatcher(cfg, p, EventStore(tmp_path / "e.db"), None, jobs, sm)
    j = jobs.create(printer="p", name="part", spool_id=4, filament_g=20, filament_m=6)
    await w.settle_spool(j, 3000.0, 3600.0)
    got = jobs.get(j["id"])
    assert got["print_duration_s"] == 3600.0
    assert got["grams_used"] == pytest.approx(10.0)
    assert got["cost"] == pytest.approx(0.2)
