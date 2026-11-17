import os
import re
import time
from pathlib import Path

import httpx
import pytest

from print_warden import calibration, check, config, validate

DOC = Path(__file__).parent.parent / "docs" / "live-validation.md"


def test_rows_match_the_runbooks_sign_off_table():
    table = {
        int(m[1]): m[2]
        for m in re.finditer(r"^\| (\d+) \| (.+?) \|", DOC.read_text(), re.MULTILINE)
    }
    assert table == validate.TITLES
    assert set(validate.ROWS) == set(validate.TITLES)
    for n, row in validate.ROWS.items():
        assert bool(row.setup) == bool(row.setup_does), n
        assert row.check or row.ask, n


class FakeWarden:
    def __init__(self, **tools):
        self.tools = tools
        self.calls = []
        self.uploads = {}
        self.pages = {}

    async def call(self, tool, **args):
        self.calls.append((tool, args))
        res = self.tools[tool]
        return res(**args) if callable(res) else res

    async def get(self, path, auth=True):
        code, text = self.pages.get((path, auth), (404, ""))
        return httpx.Response(
            code, text=text, request=httpx.Request("GET", "http://w" + path)
        )

    async def upload(self, name, data):
        self.uploads[name] = data


@pytest.fixture
def cfg():
    return config.load(os.environ["PRINT_WARDEN_CONFIG"])


@pytest.fixture
def results(tmp_path):
    return validate.Results.load(tmp_path / "validation.json")


def _ctx(cfg, results, w, n, printer="testp", **kw):
    return validate.Ctx(cfg, w, printer, results.scratch(n, printer), results, **kw)


async def _run(ctx, n, setup=False, wait=None, confirm=True):
    lines = []
    rc = await validate.run_row(n, ctx, setup, wait, lambda _: confirm, lines.append)
    return rc, "\n".join(lines)


async def test_row_0_runs_check_and_records_every_printer(cfg, results):
    async def run_check(c, **kw):
        return [
            check.Result(check.FAIL, "spoolman", "down"),
            check.Result(check.OK, "x", "y"),
        ]

    ctx = _ctx(cfg, results, FakeWarden(), 0, run_check=run_check)
    rc, out = await _run(ctx, 0)
    assert rc == 1 and "spoolman: down" in out
    assert results.get(0, "testq")["result"] == "fail"

    async def all_ok(c, **kw):
        return [check.Result(check.OK, "x", "y")]

    ctx = _ctx(cfg, results, FakeWarden(), 0, run_check=all_ok, by="EM")
    assert (await _run(ctx, 0))[0] == 0
    assert results.get(0, "testp") | {"at": 0} == {
        "at": 0,
        "result": "signed",
        "by": "EM",
        "checks": [{"label": "print-warden check: 0 failed", "ok": True, "detail": ""}],
    }
    saved = validate.Results.load(results.path)
    assert validate.cell(saved.get(0, "testq")).endswith(" EM")


async def test_row_1_stages_a_cube_and_checks_it(cfg, results):
    job = {
        "id": "j1",
        "name": "validate_cube",
        "material": "PLA",
        "quality": "standard",
        "state": "staged",
        "est_seconds": 600,
        "slice_blockers": [],
        "preview_url": "http://w/files/jobs/p.png",
        "log": [],
    }
    w = FakeWarden(
        loaded_material={"material": "PLA"},
        submit_job=job,
        job_status=job,
    )
    w.pages[("http://w/files/jobs/p.png", False)] = (200, "png")
    ctx = _ctx(cfg, results, w, 1)
    rc, out = await _run(ctx, 1, setup=True)
    assert rc == 0, out
    _, args = next(c for c in w.calls if c[0] == "submit_job")
    assert args["offer"] is False and args["model"] == validate.CUBE
    assert validate.CUBE in w.uploads
    assert results.scratch(1, "testp")["job_id"] == "j1"

    w.calls.clear()
    rc, out = await _run(ctx, 1, setup=True, confirm=False)
    assert "Setup skipped" in out and not [c for c in w.calls if c[0] == "submit_job"]


async def test_row_3_passes_only_when_refused(cfg, results):
    def refuse(**kw):
        raise validate.WardenError(
            "validate_oversized.stl cannot be printed on M: footprint 2000x2000 mm does not fit the 300x300 mm bed"
        )

    w = FakeWarden(loaded_material={"material": None}, submit_job=refuse)
    assert (await _run(_ctx(cfg, results, w, 3), 3))[0] == 0
    stl = w.uploads[validate.OVERSIZED]
    assert len(stl) > 84

    w = FakeWarden(
        loaded_material={"material": None}, submit_job={"id": "oops"}, skip_job={}
    )
    rc, _ = await _run(_ctx(cfg, results, w, 3), 3)
    assert (
        rc == 1
        and (
            "skip_job",
            {"job_id": "oops", "reason": "validate row 3: should have been refused"},
        )
        in w.calls
    )


async def test_row_39_fault_test_end_to_end(cfg, results):
    fault = {
        "id": "f1",
        "state": "awaiting_approval",
        "queue": {"action": "offered"},
        "log": [],
    }
    w = FakeWarden(
        fault_test=fault,
        job_status=lambda job_id: fault,
        print_status={"extruder": {"target": 215}, "bed": {"target": 60}},
        list_jobs=[],
    )
    ctx = _ctx(cfg, results, w, 39, by="EM")
    rc, out = await _run(ctx, 39, setup=True, wait=0)
    assert rc == 1 and "FAIL stopped with an error" in out
    assert w.calls[0] == (
        "fault_test",
        {"printer": "testp", "requested_by": "validate row 39 (EM)"},
    )

    fault.update(
        state="failed",
        failure_cause="move_out_of_range",
        failure_detail="Move out of range",
    )
    w.tools["print_status"] = {"extruder": {"target": 0}, "bed": {"target": 0.0}}
    w.tools["list_jobs"] = [
        {"id": "r1", "reprint_of": "f1", "state": "awaiting_approval"}
    ]
    rc, out = await _run(ctx, 39, wait=0)
    assert rc == 0, out
    assert results.get(39, "testp")["result"] == "signed"


async def test_row_38_pauses_only_a_running_warden_print(cfg, results):
    sent = []

    async def gcode(base, script):
        sent.append((base, script))

    w = FakeWarden(print_status={"state": "standby"}, list_jobs=[])
    ctx = _ctx(cfg, results, w, 38, gcode=gcode)
    rc, out = await _run(ctx, 38, setup=True)
    assert rc == 1 and "not printing" in out and not sent

    job = {"id": "p1", "name": "cube", "state": "printing", "log": []}
    w = FakeWarden(
        print_status={
            "state": "printing",
            "extruder": {"target": 200},
            "bed": {"target": 60},
        },
        list_jobs=[job],
        job_status=lambda job_id: job,
    )
    ctx = _ctx(cfg, results, w, 38, gcode=gcode)
    rc, out = await _run(ctx, 38, setup=True, wait=0)
    assert sent == [(cfg.printers["testp"].moonraker, "PAUSE")]
    assert rc == 1 and "FAIL cancelled by the deadline" in out
    assert results.scratch(38, "testp")["job_id"] == "p1"


async def test_poll_keeps_checking_until_the_checks_pass():
    seen = []

    async def checker(ctx):
        seen.append(1)
        return [validate.Check("x", len(seen) >= 3)]

    naps = []

    async def sleep(s):
        naps.append(s)

    row = validate.Row(ask="tap", check=checker)
    checks = await validate.poll(row, None, 5, sleep)
    assert checks[0].ok and len(seen) == 3 and naps == [validate.POLL_S] * 2
    seen.clear()
    assert len(await validate.poll(row, None, 0, sleep)) == 1 and len(seen) == 1


def test_sign_listing_and_report(cfg, capsys):
    path = cfg.storage_dir / "validation.json"
    path.unlink(missing_ok=True)
    assert validate.main(["sign", "9", "--printer", "testp", "--by", "EM"]) == 0
    assert (
        validate.main(["sign", "16", "--printer", "testq", "--issue", "https://x/1"])
        == 0
    )
    assert validate.main(["sign", "16", "--printer", "testq"]) == 2
    assert validate.main(["sign", "9", "--printer", "nope", "--by", "EM"]) == 2
    assert validate.main(["99"]) == 2
    assert validate.main(["4"]) == 2
    capsys.readouterr()
    assert validate.main([]) == 0
    listing = capsys.readouterr().out
    assert "2 of 92 signed or with an issue" in listing
    assert validate.main(["report"]) == 0
    rep = capsys.readouterr().out
    day = time.strftime("%Y-%m-%d")
    assert f"| 9 | HA entities follow the print | {day} EM |  |" in rep
    assert (
        "| 16 | Quiet hours / quiet entity hold warnings, not criticals |  | [issue](https://x/1) |"
        in rep
    )
    path.unlink()


async def test_mcp_warden_against_the_real_server():
    from print_warden import server

    transport = httpx.ASGITransport(app=server.asgi)
    async with server.mcp.session_manager.run():
        async with validate.McpWarden("http://warden", "test-token", transport) as w:
            names = [p["name"] for p in await w.call("list_printers")]
            assert names == ["testp", "testq"]
            with pytest.raises(validate.WardenError, match="nope"):
                await w.call("job_status", job_id="nope")
            assert (await w.call("loaded_material", printer="testp"))[
                "printer"
            ] == "testp"
            await w.upload(validate.CUBE, calibration.tower_stl(1, 20.0))
            assert (server.UPLOADS / validate.CUBE).exists()
            assert (await w.get("/healthz", auth=False)).json()["printers"]["testp"]
            assert (await w.get("/status", auth=False)).status_code == 401
            assert (await w.get("/status")).status_code == 200
        with pytest.raises(BaseException):  # noqa: B017 - the MCP client wraps the 401 in an exception group
            async with validate.McpWarden("http://warden", "wrong", transport):
                pass
