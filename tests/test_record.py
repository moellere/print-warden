"""print-warden record, and the simulator replaying what was recorded."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from print_warden import record, sim

RECORDINGS = sorted((Path(__file__).parent / "data" / "recordings").glob("*.jsonl"))


async def _stream(messages):
    for m in messages:
        yield m


async def test_capture_anonymizes_and_stops_at_the_end_of_the_print():
    lines = []
    clock = iter(range(100))
    messages = [
        {"method": "notify_status_update", "params": [{"print_stats": {"state": "printing", "filename": "warden/abc_benchy_PLA.gcode"}}, 1]},
        {"method": "notify_status_update", "params": [{"extruder": {"target": 215}}, 2]},
        {"method": "notify_status_update", "params": [{"print_stats": {"state": "error", "message": "Move out of range"}}, 3]},
        {"method": "notify_status_update", "params": [{"extruder": {"target": 0}}, 4]},
    ]  # fmt: skip

    async def store():
        return [
            {
                "message": "SDCARD_PRINT_FILE FILENAME=warden/abc_benchy_PLA.gcode",
                "type": "command",
            }
        ]

    async def history():
        return [{"filename": "warden/abc_benchy_PLA.gcode", "status": "error"}]

    ended = await record.capture(
        _stream(messages), {"print_stats": {"state": "standby", "filename": ""}}, lines.append,
        store, history, {"printer": "x4"}, clock=lambda: float(next(clock)),
    )  # fmt: skip
    assert ended == "error"
    kinds = [e["kind"] for e in lines]
    assert kinds == [
        "meta",
        "snapshot",
        "status",
        "status",
        "status",
        "gcode_store",
        "history",
    ]
    text = json.dumps(lines)
    assert "benchy" not in text and "warden/recorded-1.gcode" in text
    assert (
        lines[-2]["data"][0]["message"]
        == "SDCARD_PRINT_FILE FILENAME=warden/recorded-1.gcode"
    )


async def test_capture_stops_at_a_shutdown():
    lines = []
    messages = [
        {
            "method": "notify_status_update",
            "params": [{"print_stats": {"state": "printing"}}, 1],
        },
        {"method": "notify_klippy_shutdown"},
        {"method": "notify_status_update", "params": [{"extruder": {"target": 0}}, 4]},
    ]

    async def empty():
        return []

    assert (
        await record.capture(_stream(messages), {}, lines.append, empty, empty, {})
        == "shutdown"
    )
    assert lines[-3] == {
        "kind": "signal",
        "t": lines[-3]["t"],
        "method": "notify_klippy_shutdown",
    }


@pytest.mark.parametrize("path", RECORDINGS, ids=[p.name for p in RECORDINGS])
async def test_every_recording_replays_to_its_recorded_end(path):
    events = record.load(path)
    end = [
        e
        for e in events
        if e["kind"] == "status" and (e["data"].get("print_stats") or {}).get("state")
    ]
    p = sim.SimPrinter("r", speed=1000, replay=events)
    p.files["warden/job_cube.gcode"] = "G28\n"
    p.start("warden/job_cube.gcode")
    await asyncio.wait_for(p._task, 10)
    want = end[-1]["data"]["print_stats"]["state"] if end else None
    assert p.state["print_stats"]["state"] == want
    assert p.state["print_stats"]["filename"] == "warden/job_cube.gcode"
    hist = next((e["data"] for e in events if e["kind"] == "history"), [])
    if hist:
        assert p.history[0]["filename"] == "warden/job_cube.gcode"


async def test_the_warden_diagnoses_a_replayed_print(tmp_path):
    """The fault and shutdown recordings, replayed under the real warden: the diagnosis comes from what was
    recorded, not from the simulator's own printing."""
    from print_warden import validate
    from tests.test_demo_e2e import Phone, _until, running_demo

    fault = Path(__file__).parent / "data" / "recordings" / "sim-fault.jsonl"
    shutdown = Path(__file__).parent / "data" / "recordings" / "sim-shutdown.jsonl"
    with running_demo(
        tmp_path, "--replay", f"demo-a={fault}", "--replay", f"demo-b={shutdown}"
    ) as (root, port, sp):
        phone = Phone(sp)
        from print_warden import config

        cfg = config.load(root / "printers.yaml")
        results = validate.Results.load(root / "data" / "validation.json")
        async with validate.McpWarden(f"http://127.0.0.1:{port}", "demo") as w:
            for printer in ("demo-a", "demo-b"):
                await w.call(
                    "mark_bed_clear",
                    printer=printer,
                    authorized_by="test: the bed is empty",
                )
                ctx = validate.Ctx(cfg, w, printer, {}, results)
                job = await validate._cube_job(ctx)
                await w.call("request_start", job_id=job["id"])
                await phone.tap(f"PRINT_WARDEN_START_{job['id']}")

                async def ended(jid=job["id"]):
                    j = await w.call("job_status", job_id=jid)
                    return j if j["state"] in ("done", "failed") else None

                if printer == "demo-a":
                    finished = await _until(ended, 30)
                    assert finished["state"] == "failed"
                    assert finished["failure_cause"] == "move_out_of_range"
                    assert "9999" in (finished["failure_detail"] or "")

            async def shut_down():
                async with httpx.AsyncClient() as c:
                    health = (await c.get(f"http://127.0.0.1:{port}/healthz")).json()
                return health["printers"]["demo-b"]["last_shutdown"]

            # the recording stops at Klipper's shutdown signal; what the job does after one is for a real recording
            assert (await _until(shut_down, 30))["cause"] == "emergency_stop"
