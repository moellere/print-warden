"""`print-warden validate`: the live-validation runbook (`docs/live-validation.md`) run against the running warden.

`validate <row> --printer P` prints what the row asks of the operator, sets up what it can with `--setup` (after a
y/N confirmation: a test cube sliced and staged, a Start offer sent, a fault test queued, a running print paused, a
batch or split submitted), then checks the result against the warden's own state, polling for `--wait` minutes.
Each run's checks are recorded in `<storage>/validation.json`; with `--by <initials>` passing checks sign the row.
A row judged by eye (or by another tool) is signed with `validate sign`, and a failure gets `--issue <url>`.
`validate` lists every row and its state per printer; `validate report` prints the runbook's sign-off table.

It reaches the warden as any MCP client does (`--url`, default this config's port on 127.0.0.1, with
PRINT_WARDEN_TOKEN), so it starts nothing: a setup that offers a job still needs the phone tap. The one write it
makes outside the warden is row 38's `PAUSE` to a printer already printing a warden job, which the warden then
cancels on its own deadline. Rows 0 and 34 run `print-warden check` in-process.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, Self

import httpx

from . import calibration, check, config, moonraker

TITLES = {
    0: "`print-warden check` reports 0 failed",
    1: "Slice and stage an STL",
    2: "3MF from Orca / Bambu Studio / PrusaSlicer, plate 2",
    3: "Oversized model refused before slicing",
    4: "Start by tap",
    5: "Expired Start tap refused",
    6: "First-layer prompt",
    7: "Completion: alert, Spoolman, timelapse, park",
    8: 'Outcome prompt labels the job; "failed before" warning',
    9: "HA entities follow the print",
    10: "Occupied bed blocks the next start",
    11: "Bed cleared → labelled check → next job offered",
    12: "Bed check sees a part",
    13: "Cancel by tap; stale Cancel ignored",
    14: "Obico pause → Resume",
    15: "Restart mid-print, settled from history",
    16: "Quiet hours / quiet entity hold warnings, not criticals",
    17: "Quiet entity off → warnings resume",
    18: "MQTT device, rename survives HA restart, unavailable when stopped",
    19: "homelab-helper `printer-offline` opens and resolves",
    20: "Maintenance due → Done resets it and the sensor",
    21: "Reorder alert once a day",
    22: "Drying reminder → Dried resets it",
    23: "submit_split shares copies; each share tap-started",
    24: "Cheap-power hold, offered when the price drops",
    25: "Shutdown cause in `/healthz` and as a homelab-helper finding",
    26: "Temperature tower → section saved to the spool and used",
    27: "Flow tower leaves the printer at 100 %",
    28: "`/status` shows both printers live; 401 without the token",
    29: "Thingiverse page → named file printed, license on the job",
    30: "Printables page → named file printed, license on the job",
    31: "Profile from Klipper: block, check, a cube printed",
    32: "Imported Orca preset bundle sliced with its settings",
    33: "`profiles diff` after an Orca upgrade",
    34: "`check --sites`: Thingiverse token and Printables query",
    35: "A 4-plate project as one batch across both printers",
    36: "`/stage`: upload, slice, queue, offer on the phone",
    37: "kWh per print within a few percent of the plug's counter",
    38: "A paused print cancels itself, heaters off; a failed print is cooled",
    39: "An errored print names its cause; Reprint queues it",
    40: "Away mode: no offer or auto-start away; summary and offer on return",
    41: "Printer health: a printer.cfg change is a finding until accepted",
    42: "Spool registered, labelled, swapped on with the old one weighed",
    43: "Weigh-in reply corrects Spoolman; a wrong density shows as drift",
    44: "Reorder on the HA to-do list and off again; the spool scale weighs",
    45: "Weekly digest arrives once; held while away",
    46: "The plan holds an evening job and offers it at its morning slot",
}
CUBE = "validate_cube.stl"
OVERSIZED = "validate_oversized.stl"
POLL_S = 15.0


class WardenError(Exception):
    """A tool call the warden refused, with its message."""


class Warden(Protocol):
    async def call(self, tool: str, **args: Any) -> Any: ...

    async def get(self, path: str, auth: bool = True) -> httpx.Response: ...

    async def upload(self, name: str, data: bytes) -> None: ...


@dataclass
class Check:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Ctx:
    cfg: config.Config
    w: Warden
    printer: str
    scratch: dict[str, Any]
    results: Results
    by: str | None = None
    model: str | None = None
    gcode: Callable[[str, str], Awaitable[None]] = moonraker.gcode_script
    run_check: Callable[..., Awaitable[list[check.Result]]] = check.run


Step = Callable[[Ctx], Awaitable[str]]
Checker = Callable[[Ctx], Awaitable[list[Check]]]


@dataclass(frozen=True)
class Row:
    ask: str = ""
    setup_does: str = ""
    setup: Step | None = None
    check: Checker | None = None
    wait_min: float = 0.0
    every_printer: bool = False
    """One run covers every printer (the check is not about one)."""

    @property
    def kind(self) -> str:
        if not self.check:
            return "manual"
        return "auto" if not self.ask else "assisted"


@dataclass
class Results:
    """`validation.json`: per row and printer the latest outcome, plus what setups left for their checks."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Results:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        data.setdefault("rows", {})
        data.setdefault("scratch", {})
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)

    def get(self, row: int, printer: str) -> dict[str, Any] | None:
        out: dict[str, Any] | None = self.data["rows"].get(str(row), {}).get(printer)
        return out

    def record(self, row: int, printer: str, entry: dict[str, Any]) -> None:
        self.data["rows"].setdefault(str(row), {})[printer] = {
            "at": time.time(),
            **entry,
        }

    def scratch(self, row: int, printer: str) -> dict[str, Any]:
        out: dict[str, Any] = self.data["scratch"].setdefault(f"{row}:{printer}", {})
        return out


def cell(entry: dict[str, Any] | None) -> str:
    """One sign-off table cell."""
    if not entry:
        return ""
    day = datetime.fromtimestamp(entry["at"]).astimezone().strftime("%Y-%m-%d")
    if entry.get("issue"):
        return f"[issue]({entry['issue']})"
    if entry["result"] == "signed":
        return f"{day} {entry['by']}"
    if entry["result"] == "pass":
        return f"checks passed {day}"
    return f"failed {day}" + (f": {entry['note']}" if entry.get("note") else "")


def _ok(label: str, cond: Any, detail: str = "") -> Check:
    return Check(label, bool(cond), detail)


async def _material(ctx: Ctx) -> tuple[str, str]:
    """(material, quality) for a test cube: the loaded material, the draft quality where there is one."""
    p = ctx.cfg.printers[ctx.printer]
    loaded = (await ctx.w.call("loaded_material", printer=ctx.printer)).get("material")
    mat = loaded if loaded in p.filaments else min(p.filaments, default="PLA")
    quality = (
        "draft" if "draft" in p.processes else min(p.processes, default="standard")
    )
    return mat, quality


async def _cube_job(ctx: Ctx) -> dict[str, Any]:
    await ctx.w.upload(CUBE, calibration.tower_stl(1, 20.0))
    mat, quality = await _material(ctx)
    job: dict[str, Any] = await ctx.w.call(
        "submit_job",
        printer=ctx.printer,
        model=CUBE,
        material=mat,
        quality=quality,
        name="validate_cube",
        offer=False,
    )
    ctx.results.scratch(1, ctx.printer)["job_id"] = job["id"]
    return job


async def _job(ctx: Ctx, job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    try:
        out: dict[str, Any] = await ctx.w.call("job_status", job_id=job_id)
    except WardenError:
        return None
    return out


def _logged(job: dict[str, Any], text: str) -> bool:
    return any(text in (e.get("note") or "") for e in job.get("log") or [])


async def _latest(ctx: Ctx, *states: str) -> dict[str, Any] | None:
    """The printer's newest job in one of `states`, with its log."""
    for j in await ctx.w.call("list_jobs", printer=ctx.printer, limit=20):
        if j["state"] in states:
            return await _job(ctx, j["id"])
    return None


def _cold(status: dict[str, Any]) -> bool:
    return all(
        float((status.get(h) or {}).get("target") or 0) == 0
        for h in ("extruder", "bed")
    )


# Row 0 and 34: print-warden check, in-process.


async def check_0(ctx: Ctx) -> list[Check]:
    res = await ctx.run_check(ctx.cfg)
    bad = [f"{r.area}: {r.detail}" for r in res if r.status == check.FAIL]
    return [_ok(f"print-warden check: {len(bad)} failed", not bad, "; ".join(bad))]


async def check_34(ctx: Ctx) -> list[Check]:
    res = {r.area: r for r in await ctx.run_check(ctx.cfg, sites=True)}
    out = []
    for site in ("thingiverse", "printables"):
        r = res.get(site)
        out.append(
            _ok(
                f"{site} model pages resolve",
                r and r.status == check.OK,
                r.detail if r else "not checked",
            )
        )
    return out


# Rows 1-13: slice, start, watch, bed, stop.


async def setup_1(ctx: Ctx) -> str:
    job = await _cube_job(ctx)
    return (
        f"staged {job['name']} as job {job['id']} ({job['material']}, {job['quality']})"
    )


async def check_1(ctx: Ctx) -> list[Check]:
    job = await _job(ctx, ctx.results.scratch(1, ctx.printer).get("job_id"))
    if not job:
        return [_ok("a test cube from `validate 1 --setup`", False)]
    preview = job.get("preview_url")
    code: int | str | None = None
    if preview:
        try:
            code = (await ctx.w.get(preview, auth=False)).status_code
        except httpx.HTTPError as e:
            code = str(e)
    return [
        _ok("staged, not started", job["state"] == "staged", job["state"]),
        _ok(
            "an estimate", (job.get("est_seconds") or 0) > 0, job.get("est_human") or ""
        ),
        _ok(
            "no blockers",
            not job.get("slice_blockers"),
            "; ".join(job.get("slice_blockers") or []),
        ),
        _ok("the preview opens", code == 200, f"{preview}: {code}"),
    ]


async def check_3(ctx: Ctx) -> list[Check]:
    await ctx.w.upload(OVERSIZED, calibration.tower_stl(1, 20.0, size=2000.0))
    mat, quality = await _material(ctx)
    try:
        job = await ctx.w.call(
            "submit_job",
            printer=ctx.printer,
            model=OVERSIZED,
            material=mat,
            quality=quality,
            name="validate_oversized",
            offer=False,
        )
    except WardenError as e:
        msg = str(e)
        return [
            _ok("refused before slicing", "cannot be printed" in msg, msg),
            _ok(
                "says what does not fit", "does not fit" in msg or "exceeds" in msg, msg
            ),
        ]
    await ctx.w.call(
        "skip_job", job_id=job["id"], reason="validate row 3: should have been refused"
    )
    return [_ok("refused before slicing", False, f"staged as {job['id']} (skipped)")]


async def setup_4(ctx: Ctx) -> str:
    job = await _job(ctx, ctx.results.scratch(1, ctx.printer).get("job_id"))
    if not job or job["state"] not in ("staged", "awaiting_approval"):
        job = await _cube_job(ctx)
    res = await ctx.w.call("request_start", job_id=job["id"])
    ctx.scratch["job_id"] = job["id"]
    return f"offered {job['id']} (notification sent: {res.get('notification_sent')})"


async def check_4(ctx: Ctx) -> list[Check]:
    job = await _job(ctx, ctx.scratch.get("job_id"))
    if not job:
        return [_ok("a job offered by `validate 4 --setup`", False)]
    return [
        _ok("started", job["state"] in ("printing", "done"), job["state"]),
        _ok("started by the tap", _logged(job, "phone tap"), "job log"),
    ]


async def setup_5(ctx: Ctx) -> str:
    out = await setup_4(ctx)
    ctx.scratch["asked_at"] = time.time()
    return out


async def check_5(ctx: Ctx) -> list[Check]:
    job = await _job(ctx, ctx.scratch.get("job_id"))
    if not job:
        return [_ok("a job offered by `validate 5 --setup`", False)]
    return [
        _ok(
            "the late tap was refused",
            _logged(job, "approval request expired"),
            "job log",
        ),
        _ok("back to staged", job["state"] == "staged", job["state"]),
    ]


async def check_6(ctx: Ctx) -> list[Check]:
    job = await _latest(ctx, "printing", "done", "failed")
    return [
        _ok(
            "Looks good recorded on the running job",
            job and _logged(job, "first layers look good"),
            job["name"] if job else "no job printing",
        )
    ]


async def check_7(ctx: Ctx) -> list[Check]:
    job = await _latest(ctx, "done", "failed")
    if not job or job["state"] != "done":
        return [_ok("the last print finished", False, job["state"] if job else "none")]
    bed = await ctx.w.call("bed_status", printer=ctx.printer)
    out = [
        _ok("finished", True, job["name"]),
        _ok(
            "grams used recorded",
            (job.get("grams_used") or 0) > 0,
            f"{job.get('grams_used')} g",
        ),
        _ok(
            "actual time recorded",
            (job.get("print_duration_s") or 0) > 0,
            f"{job.get('print_duration_s')} s",
        ),
        _ok(
            "a timelapse", job.get("timelapse_url"), job.get("timelapse_url") or "none"
        ),
    ]
    if bed.get("pose"):
        out.append(
            _ok(
                "parked at the bed-check pose",
                (bed.get("at_pose") or {}).get("ok"),
                str(bed.get("at_pose")),
            )
        )
    return out


async def check_8(ctx: Ctx) -> list[Check]:
    jobs = await ctx.w.call("list_jobs", printer=ctx.printer, limit=20)
    labelled = next((j for j in jobs if j.get("outcome")), None)
    if not labelled:
        return [
            _ok(
                "a labelled job",
                False,
                "tap Failed → More… → Layer shift on an outcome prompt",
            )
        ]
    again = [
        j
        for j in jobs
        if j["created"] > labelled["created"]
        and j.get("model_hash") == labelled.get("model_hash")
    ]
    return [
        _ok("labelled failed", labelled["outcome"] == "failed", labelled["outcome"]),
        _ok(
            "reason layer_shift",
            labelled.get("outcome_reason") == "layer_shift",
            str(labelled.get("outcome_reason")),
        ),
        _ok(
            'the re-submit warns "failed before"',
            any("failed before" in w for j in again for w in j.get("warnings") or []),
            f"{len(again)} later job(s) of the same model",
        ),
    ]


async def check_10(ctx: Ctx) -> list[Check]:
    bed = await ctx.w.call("bed_status", printer=ctx.printer)
    job = await _latest(ctx, "staged", "awaiting_approval")
    blockers = (job or {}).get("blockers") or []
    return [
        _ok("bed occupied", bed["state"] == "occupied", bed["state"]),
        _ok("a staged job", job, "stage one with `validate 1 --setup`"),
        _ok(
            "its start is blocked by the bed",
            any("bed" in b for b in blockers),
            "; ".join(blockers),
        ),
    ]


async def check_11(ctx: Ctx) -> list[Check]:
    bed = await ctx.w.call("bed_status", printer=ctx.printer)
    recent = bed.get("recent") or []
    offered = await _latest(ctx, "awaiting_approval", "printing")
    return [
        _ok("bed clear", bed["state"] == "clear", bed["state"]),
        _ok(
            "a labelled check recorded",
            recent and recent[0].get("label"),
            str(recent[:1]),
        ),
        _ok(
            "the next job offered",
            offered,
            offered["name"] if offered else "nothing queued",
        ),
    ]


async def check_12(ctx: Ctx) -> list[Check]:
    try:
        res = await ctx.w.call("check_bed", printer=ctx.printer)
    except WardenError as e:
        return [_ok("a bed check ran", False, str(e))]
    return [
        _ok(
            "not clear with the part on",
            not res["clear"],
            f"score {res['score']} (threshold {res['threshold']})",
        )
    ]


async def check_13(ctx: Ctx) -> list[Check]:
    job = next(
        (
            j
            for j in await ctx.w.call("list_jobs", printer=ctx.printer, limit=20)
            if j["state"] in ("failed", "done", "printing")
            and not calibration.is_fault(j)
        ),
        None,
    )
    why = f"{(job or {}).get('failure_cause')} / {(job or {}).get('error')}"
    return [
        _ok(
            "the last print failed",
            job and job["state"] == "failed",
            job["state"] if job else "none",
        ),
        _ok("as cancelled", job and "cancel" in why.lower(), why),
    ]


# Rows 23, 25, 28, 35, 37: scheduling, shutdown, status page, batch, energy.


def _location(ctx: Ctx) -> str:
    return ctx.cfg.printers[ctx.printer].location or ctx.printer


async def setup_23(ctx: Ctx) -> str:
    await ctx.w.upload(CUBE, calibration.tower_stl(1, 20.0))
    mat, quality = await _material(ctx)
    res = await ctx.w.call(
        "submit_split",
        where=_location(ctx),
        model=CUBE,
        material=mat,
        copies=6,
        quality=quality,
    )
    ctx.scratch["job_ids"] = [j["id"] for j in res["jobs"]]
    return f"split into {len(res['jobs'])} job(s): {res.get('plan')}"


async def check_23(ctx: Ctx) -> list[Check]:
    jobs = [
        j for j in [await _job(ctx, i) for i in ctx.scratch.get("job_ids") or []] if j
    ]
    if not jobs:
        return [_ok("jobs from `validate 23 --setup`", False)]
    printers = sorted({j["printer"] for j in jobs})
    copies = sum(int(j.get("copies") or 1) for j in jobs)
    tapped = [j for j in jobs if j["state"] in ("printing", "done", "failed")]
    return [
        _ok("shared across printers", len(printers) >= 2, ", ".join(printers)),
        _ok("six copies in all", copies == 6, str(copies)),
        _ok(
            "each started by its tap",
            tapped and all(_logged(j, "phone tap") for j in tapped),
            f"{len(tapped)} of {len(jobs)} started",
        ),
    ]


async def check_25(ctx: Ctx) -> list[Check]:
    h = (await ctx.w.get("/healthz", auth=False)).json()
    last = h["printers"][ctx.printer].get("last_shutdown")
    return [
        _ok("/healthz names the shutdown cause", last and last.get("cause"), str(last))
    ]


async def check_28(ctx: Ctx) -> list[Check]:
    page = await ctx.w.get("/status")
    bare = await ctx.w.get("/status", auth=False)
    return [
        _ok("/status with the token", page.status_code == 200, str(page.status_code)),
        _ok(
            "every printer on it",
            all(n in page.text for n in ctx.cfg.printers),
            ", ".join(n for n in ctx.cfg.printers if n not in page.text),
        ),
        _ok(
            "401 without the token",
            bare.status_code == 401 or not ctx.cfg.api_token,
            str(bare.status_code)
            + ("" if ctx.cfg.api_token else " (the warden runs without a token)"),
        ),
    ]


async def setup_35(ctx: Ctx) -> str:
    mat, _ = await _material(ctx)
    if ctx.model:
        items = [{"model": ctx.model, "material": mat, "plates": "all"}]
    else:
        await ctx.w.upload(CUBE, calibration.tower_stl(1, 20.0))
        items = [{"model": CUBE, "material": mat} for _ in range(4)]
    res = await ctx.w.call(
        "submit_batch", where=_location(ctx), items=items, name="validate batch"
    )
    ctx.scratch["batch_id"] = res["batch_id"]
    return f"batch {res['batch_id']}: {len(res['jobs'])} job(s) on {sorted({j['printer'] for j in res['jobs']})}"


async def check_35(ctx: Ctx) -> list[Check]:
    bid = ctx.scratch.get("batch_id")
    if not bid:
        return [_ok("a batch from `validate 35 --setup`", False)]
    b = await ctx.w.call("batch_status", batch_id=bid)
    printers = sorted({j["printer"] for j in b["jobs"]})
    return [
        _ok("four jobs", b["total"] == 4, str(b["total"])),
        _ok("across both printers", len(printers) >= 2, ", ".join(printers)),
        _ok(
            "on /status",
            "validate batch" in (await ctx.w.get("/status")).text,
            "batch line",
        ),
    ]


async def check_37(ctx: Ctx) -> list[Check]:
    job = await _latest(ctx, "done")
    if not job:
        return [_ok("a finished print", False)]
    kwh, heat = job.get("energy_kwh"), job.get("heatup_kwh")
    return [
        _ok("kWh recorded", (kwh or 0) > 0, f"{kwh} kWh"),
        _ok(
            "heat-up under the total",
            heat is not None and kwh and heat < kwh,
            f"{heat} kWh",
        ),
        _ok("a cost", job.get("energy_cost") is not None, str(job.get("energy_cost"))),
    ]


# Rows 38, 39, 41: pause deadline, fault test, printer health.


async def setup_38(ctx: Ctx) -> str:
    st = await ctx.w.call("print_status", printer=ctx.printer)
    job = await _latest(ctx, "printing")
    if st.get("state") != "printing" or not job:
        raise WardenError(
            f"{ctx.printer} is not printing a warden job ({st.get('state')})"
        )
    await ctx.gcode(ctx.cfg.printers[ctx.printer].moonraker, "PAUSE")
    ctx.scratch.update(job_id=job["id"], paused_at=time.time())
    return f"paused {job['name']} ({job['id']})"


async def check_38(ctx: Ctx) -> list[Check]:
    job = await _job(ctx, ctx.scratch.get("job_id"))
    if not job:
        return [_ok("a print paused by `validate 38 --setup`", False)]
    st = await ctx.w.call("print_status", printer=ctx.printer)
    return [
        _ok(
            "cancelled by the deadline",
            _logged(job, "paused with no answer"),
            job["state"],
        ),
        _ok(
            "heaters off",
            _cold(st),
            f"extruder {st.get('extruder')}, bed {st.get('bed')}",
        ),
    ]


async def setup_39(ctx: Ctx) -> str:
    job = await ctx.w.call(
        "fault_test",
        printer=ctx.printer,
        requested_by="validate row 39" + (f" ({ctx.by})" if ctx.by else ""),
    )
    ctx.scratch["job_id"] = job["id"]
    return f"queued the fault test {job['id']}: {job.get('queue')}"


async def check_39(ctx: Ctx) -> list[Check]:
    job = await _job(ctx, ctx.scratch.get("job_id"))
    if not job:
        return [_ok("a fault test from `validate 39 --setup`", False)]
    st = await ctx.w.call("print_status", printer=ctx.printer)
    jobs = await ctx.w.call("list_jobs", printer=ctx.printer, limit=20)
    again = [j for j in jobs if j.get("reprint_of") == job["id"]]
    return [
        _ok("stopped with an error", job["state"] == "failed", job["state"]),
        _ok(
            "cause: move out of range",
            job.get("failure_cause") == "move_out_of_range",
            str(job.get("failure_detail")),
        ),
        _ok(
            "heaters off",
            _cold(st),
            f"extruder {st.get('extruder')}, bed {st.get('bed')}",
        ),
        _ok(
            "Reprint queued it again",
            again,
            again[0]["id"] if again else "tap Reprint on the prompt",
        ),
    ]


async def setup_41(ctx: Ctx) -> str:
    res = await ctx.w.call("printer_health", printer=ctx.printer)
    return f"health snapshot taken; findings now: {[f['kind'] for f in res.get('findings') or []]}"


async def check_41(ctx: Ctx) -> list[Check]:
    res = await ctx.w.call("printer_health", printer=ctx.printer)
    changed = [
        f["detail"] for f in res.get("findings") or [] if f["kind"] == "config_changed"
    ]
    h = (await ctx.w.get("/healthz", auth=False)).json()
    kinds = h["printers"][ctx.printer].get("health") or []
    return [
        _ok("printer_health lists the change", changed, "; ".join(changed)),
        _ok(
            "/healthz shows config_changed (after the hourly pass)",
            "config_changed" in kinds,
            str(kinds),
        ),
    ]


async def check_45(ctx: Ctx) -> list[Check]:
    sent = [
        e
        for e in await ctx.w.call("get_events", since_minutes=8 * 24 * 60, limit=200)
        if e["kind"] == "weekly_digest"
    ]
    return [
        _ok(
            "a weekly digest went out this week",
            sent,
            sent[0]["message"][:120] if sent else "none yet",
        )
    ]


ROWS: dict[int, Row] = {
    0: Row(check=check_0, every_printer=True),
    1: Row(
        setup_does="upload a 20 mm cube and slice it, staged but not offered",
        setup=setup_1,
        check=check_1,
    ),
    2: Row(
        ask="Submit a 3MF from each slicer, one with two plates (plate=2), one with a negative part."
    ),
    3: Row(check=check_3),
    4: Row(
        ask="Tap Start on the phone.",
        setup_does="send the Start / Skip offer for the row 1 cube (slicing one if needed)",
        setup=setup_4,
        check=check_4,
        wait_min=15,
    ),
    5: Row(
        ask="Wait until the approval TTL has passed, then tap Start on that notification.",
        setup_does="send a Start / Skip offer for a test cube",
        setup=setup_5,
        check=check_5,
        wait_min=30,
    ),
    6: Row(
        ask="At layer 2, tap Looks good on the first-layer prompt.",
        check=check_6,
        wait_min=20,
    ),
    7: Row(
        ask="Let the print finish; check the completion alert and that the spool is lighter by about the job's grams.",
        check=check_7,
        wait_min=60,
    ),
    8: Row(
        ask="Tap Failed → More… → Layer shift on the outcome prompt, then re-submit the same model and settings.",
        check=check_8,
        wait_min=15,
    ),
    9: Row(
        ask="Watch sensor.print_warden_<printer>_* (or the MQTT device) through a print."
    ),
    10: Row(ask="Leave the part on the bed with a job staged.", check=check_10),
    11: Row(ask="Remove the part, tap Bed cleared.", check=check_11, wait_min=10),
    12: Row(ask="Leave a part on the bed.", check=check_12),
    13: Row(
        ask="Mid-print, tap Cancel print on the first-layer prompt.",
        check=check_13,
        wait_min=15,
    ),
    14: Row(ask="Pause from Obico; tap Resume on the phone."),
    15: Row(
        ask="Restart the warden mid-print; watch the job settle from Moonraker's history."
    ),
    16: Row(ask="Inside quiet hours, raise a warning and a critical."),
    17: Row(ask="Toggle the quiet entity off and raise a warning."),
    18: Row(
        ask="Check the MQTT device in HA, rename a sensor, restart HA, stop the warden."
    ),
    19: Row(
        ask="Run `helper discover printwarden --persist` with a printer off, then on."
    ),
    20: Row(
        ask="Set a maintenance item to every_hours: 0.1, finish a print, tap Done."
    ),
    21: Row(ask="Set upkeep.reorder_below_g above a material's stock."),
    22: Row(ask="Load a TPU spool first used more than dry_after_days ago; tap Dried."),
    23: Row(
        ask="Tap Start on each share's offer.",
        setup_does="split six 20 mm cubes across the location's idle printers (each share offered for a tap)",
        setup=setup_23,
        check=check_23,
        wait_min=15,
    ),
    24: Row(
        ask="With policy.cheap_power and the price above max_price, queue a long job."
    ),
    25: Row(
        ask="Send M112 in the console (then FIRMWARE_RESTART once checked).",
        check=check_25,
        wait_min=5,
    ),
    26: Row(
        ask="set_active_spool, calibrate(printer, 'temp'), print it and pick a section."
    ),
    27: Row(ask="calibrate(printer, 'flow'), then a normal print; M221 reports 100 %."),
    28: Row(check=check_28, every_printer=True),
    29: Row(
        ask="model_page + submit_job from a Thingiverse page (THINGIVERSE_TOKEN set)."
    ),
    30: Row(ask="model_page + submit_job from a Printables page."),
    31: Row(
        ask="`print-warden profiles klipper`, paste the block, check, print a cube."
    ),
    32: Row(
        ask="Export an Orca preset bundle, `print-warden profiles import`, slice with it."
    ),
    33: Row(ask="`print-warden profiles diff` before and after an Orca image upgrade."),
    34: Row(check=check_34, every_printer=True),
    35: Row(
        ask="Tap Start on each job's offer.",
        setup_does="submit a batch: four 20 mm cubes, or every plate of --model",
        setup=setup_35,
        check=check_35,
        wait_min=10,
    ),
    36: Row(
        ask="Open /stage on a phone, upload, slice, add to queue; post a two-hour-old form."
    ),
    37: Row(ask="Print with power_entity set.", check=check_37),
    38: Row(
        ask="Leave the paused print alone (set policy.paused_cancel_minutes low for the test).",
        setup_does="send PAUSE to the printer's running warden print",
        setup=setup_38,
        check=check_38,
        wait_min=40,
    ),
    39: Row(
        ask="Tap Start; when the prompt names the cause, tap Reprint, then Skip the reprint's offer.",
        setup_does="queue the fault test: a 20 mm cube that stops itself at layer 2 (offered for a tap)",
        setup=setup_39,
        check=check_39,
        wait_min=30,
    ),
    40: Row(ask="Leave home with a 2 h job queued and auto_start on; come back."),
    41: Row(
        ask="Change max_accel in printer.cfg and restart Klipper; then printer_health(accept=True).",
        setup_does="take the printer-health snapshot (printer_health)",
        setup=setup_41,
        check=check_41,
        wait_min=70,
    ),
    42: Row(
        ask="Register a spool on /spools, label it, swap it on with the outgoing spool on the scale."
    ),
    43: Row(
        ask="With upkeep.weigh_every_days: 1, answer the weigh-in prompt with the scale reading."
    ),
    44: Row(
        ask="With upkeep.todo_entity set, let a material run under reorder_below_g; lift the spool off the scale."
    ),
    45: Row(
        ask="Set digest to today, an hour or two ahead; restart and wait past it.",
        check=check_45,
        wait_min=150,
    ),
    46: Row(
        ask="Queue a long job in the evening with plan_offers on; check plan_week, then wait for the morning offer."
    ),
}


class McpWarden:
    """The running warden over its MCP endpoint and HTTP routes."""

    def __init__(
        self,
        url: str,
        token: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.transport = transport
        self.http = httpx.AsyncClient(transport=transport, timeout=120)
        self._stack = contextlib.AsyncExitStack()
        self._session: Any = None

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def __aenter__(self) -> Self:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        try:
            client = await self._stack.enter_async_context(
                httpx.AsyncClient(
                    transport=self.transport, headers=self._auth(), timeout=300
                )
            )
            read, write, _ = await self._stack.enter_async_context(
                streamable_http_client(f"{self.url}/mcp", http_client=client)
            )
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()
        await self.http.aclose()

    async def call(self, tool: str, **args: Any) -> Any:
        res = await self._session.call_tool(tool, args)
        text = " ".join(getattr(c, "text", "") for c in res.content)
        if res.isError:
            raise WardenError(text)
        sc = res.structuredContent
        if sc is None:
            return json.loads(text) if text else None
        return sc["result"] if set(sc) == {"result"} else sc

    async def get(self, path: str, auth: bool = True) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.url}{path}"
        return await self.http.get(url, headers=self._auth() if auth else {})

    async def upload(self, name: str, data: bytes) -> None:
        r = await self.http.post(
            f"{self.url}/upload", files={"file": (name, data)}, headers=self._auth()
        )
        r.raise_for_status()


async def poll(
    row: Row,
    ctx: Ctx,
    wait_min: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[Check]:
    """Run the row's checks until they all pass or `wait_min` runs out; the last run's checks."""
    assert row.check
    deadline = time.monotonic() + wait_min * 60
    while True:
        checks = await row.check(ctx)
        if all(c.ok for c in checks) or time.monotonic() >= deadline:
            return checks
        await sleep(min(POLL_S, max(0.0, deadline - time.monotonic())))


async def run_row(
    n: int,
    ctx: Ctx,
    setup: bool,
    wait_min: float | None,
    confirm: Callable[[str], bool],
    out: Callable[[str], None] = print,
) -> int:
    """One row on one printer: 0 when its checks pass (or there was nothing to check), 1 when they fail."""
    row = ROWS[n]
    out(f"Row {n}: {TITLES[n]} ({row.kind}; steps in docs/live-validation.md)")
    if setup:
        if not row.setup:
            out("This row has no setup.")
        elif not confirm(f"Set up on {ctx.printer}: {row.setup_does}. Go ahead?"):
            out("Setup skipped.")
        else:
            try:
                out("Setup: " + await row.setup(ctx))
            except (WardenError, httpx.HTTPError, ValueError) as e:
                out(f"Setup failed: {e}")
                ctx.results.save()
                return 1
            ctx.results.save()
    if row.ask:
        out(f"Operator: {row.ask}")
    if not row.check:
        out(
            f"Nothing to check automatically: sign it with `print-warden validate sign {n} --printer {ctx.printer} --by <initials>`."
        )
        return 0
    wait = row.wait_min if wait_min is None else wait_min
    if wait and row.ask:
        out(f"Checking for up to {wait:g} min (Ctrl-C stops)...")
    try:
        checks = await poll(row, ctx, wait if row.ask else 0)
    except (WardenError, httpx.HTTPError) as e:
        checks = [Check("the warden answered", False, str(e))]
    for c in checks:
        out(
            f"  {'ok  ' if c.ok else 'FAIL'} {c.label}"
            + (f": {c.detail}" if c.detail else "")
        )
    passed = all(c.ok for c in checks)
    entry = {
        "result": ("signed" if ctx.by else "pass") if passed else "fail",
        "checks": [vars(c) for c in checks],
        **({"by": ctx.by} if ctx.by and passed else {}),
    }
    for p in ctx.cfg.printers if row.every_printer else [ctx.printer]:
        ctx.results.record(n, p, entry)
    ctx.results.save()
    if passed:
        out(
            "Passed"
            + (f", signed {ctx.by}." if ctx.by else ": add --by <initials> to sign it.")
        )
    else:
        out(
            f"Failed. Open an issue and record it: `print-warden validate sign {n} --printer {ctx.printer} --issue <url>`."
        )
    return 0 if passed else 1


def listing(cfg: config.Config, results: Results) -> str:
    names = list(cfg.printers)
    lines = [f"{'#':>3} {'kind':<9} " + " ".join(f"{p:<22}" for p in names) + " row"]
    for n, title in TITLES.items():
        cells = " ".join(f"{cell(results.get(n, p)) or '-':<22}" for p in names)
        lines.append(f"{n:>3} {ROWS[n].kind:<9} {cells} {title}")
    done = sum(
        1
        for n in TITLES
        for p in names
        if (e := results.get(n, p)) and (e["result"] == "signed" or e.get("issue"))
    )
    lines.append(f"\n{done} of {len(TITLES) * len(names)} signed or with an issue")
    return "\n".join(lines)


def report(cfg: config.Config, results: Results) -> str:
    names = list(cfg.printers)
    lines = [
        "| # | Row | " + " | ".join(names) + " |",
        "|---|---|" + "---|" * len(names),
    ]
    for n, title in TITLES.items():
        lines.append(
            f"| {n} | {title} | "
            + " | ".join(cell(results.get(n, p)) for p in names)
            + " |"
        )
    return "\n".join(lines)


def _yes(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


class Operator(Protocol):
    """Where a session's questions go and its answers come from: the phone, or the terminal."""

    async def ask(
        self,
        title: str,
        message: str,
        choices: list[str],
        note_choice: str | None = None,
    ) -> str: ...

    async def answer(self, handle: str) -> tuple[str, str | None] | None: ...

    async def tell(self, title: str, message: str) -> None: ...


class PhoneOperator:
    """Questions as phone notifications with buttons (`ask_operator`); answers read back with `operator_answer`."""

    def __init__(self, w: Warden, printer: str) -> None:
        self.w = w
        self.printer = printer

    async def ask(
        self,
        title: str,
        message: str,
        choices: list[str],
        note_choice: str | None = None,
    ) -> str:
        res = await self.w.call(
            "ask_operator",
            title=title,
            message=message,
            choices=choices,
            note_choice=note_choice,
        )
        if not res.get("sent"):
            raise WardenError(
                "the phone prompt was not sent: is Home Assistant's notify_service set?"
            )
        return str(res["prompt_id"])

    async def answer(self, handle: str) -> tuple[str, str | None] | None:
        res = await self.w.call("operator_answer", prompt_id=handle)
        return (res["choice"], res.get("note")) if res.get("answered") else None

    async def tell(self, title: str, message: str) -> None:
        await self.w.call(
            "send_alert",
            printer=self.printer,
            message=f"{title}: {message}",
            severity="info",
        )


class TerminalOperator:
    """The same questions at the terminal, for a session run without the phone (`--terminal`)."""

    def __init__(
        self, out: Callable[[str], None] = print, read: Callable[[str], str] = input
    ) -> None:
        self.out = out
        self.read = read
        self.pending: dict[str, asyncio.Task[tuple[str, str | None]]] = {}

    async def ask(
        self,
        title: str,
        message: str,
        choices: list[str],
        note_choice: str | None = None,
    ) -> str:
        self.out(f"{title}\n  {message}")

        def blocking() -> tuple[str, str | None]:
            while True:
                got = self.read(f"  [{' / '.join(choices)}] ").strip().lower()
                pick = next(
                    (c for c in choices if c.lower().startswith(got) and got), None
                )
                if pick:
                    note = (
                        self.read("  note: ").strip() or None
                        if pick == note_choice
                        else None
                    )
                    return pick, note

        handle = f"t{len(self.pending)}"
        self.pending[handle] = asyncio.create_task(asyncio.to_thread(blocking))
        return handle

    async def answer(self, handle: str) -> tuple[str, str | None] | None:
        task = self.pending[handle]
        return task.result() if task.done() else None

    async def tell(self, title: str, message: str) -> None:
        self.out(f"{title}: {message}")


async def _wait(
    op: Operator, handle: str, sleep: Callable[[float], Awaitable[None]]
) -> tuple[str, str | None]:
    while (got := await op.answer(handle)) is None:
        await sleep(POLL_S)
    return got


def _rows(spec: str | None) -> list[int]:
    """`1-10,12` -> [1..10, 12]; none -> every row."""
    if not spec:
        return list(TITLES)
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        out += range(int(a), int(b or a) + 1)
    return [n for n in out if n in TITLES]


def _settled(results: Results, n: int, printer: str) -> bool:
    e = results.get(n, printer)
    return bool(e and (e["result"] == "signed" or e.get("issue")))


async def session(
    rows: list[int],
    make_ctx: Callable[[int], Ctx],
    op: Operator,
    by: str,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, list[int]]:
    """Every row in order, each through the operator: a setup only on Go, a checkable row polled until it passes
    (with Skip on the phone meanwhile), a failing one offered Retry / Fail (with a note) / Skip, and a row judged by
    eye asked Pass / Fail / Skip. Signed rows and rows with an issue are passed over, so a session resumes."""
    done: dict[str, list[int]] = {
        "signed": [],
        "failed": [],
        "skipped": [],
        "already": [],
    }
    for n in rows:
        ctx = make_ctx(n)
        if _settled(ctx.results, n, ctx.printer):
            done["already"].append(n)
            continue
        row = ROWS[n]
        head = f"Row {n}: {TITLES[n]}"
        out(head)
        if row.setup:
            choice, _ = await _wait(
                op,
                await op.ask(head, f"Set up: {row.setup_does}?", ["Go", "Skip"]),
                sleep,
            )
            if choice == "Skip":
                done["skipped"].append(n)
                continue
            try:
                out("  setup: " + await row.setup(ctx))
                ctx.results.save()
            except (WardenError, httpx.HTTPError, ValueError) as e:
                out(f"  setup failed: {e}")
        result, note, checks = await _judge(n, row, ctx, op, head, out, sleep)
        if result == "skipped":
            done["skipped"].append(n)
            continue
        entry: dict[str, Any] = {
            "result": "signed" if result == "pass" else "fail",
            "checks": [vars(c) for c in checks],
        }
        if result == "pass":
            entry["by"] = by
        if note:
            entry["note"] = note
        for p in ctx.cfg.printers if row.every_printer else [ctx.printer]:
            ctx.results.record(n, p, entry)
        ctx.results.save()
        done["signed" if result == "pass" else "failed"].append(n)
        out(
            f"  {'signed ' + by if result == 'pass' else 'failed' + (': ' + note if note else '')}"
        )
    return done


async def _judge(
    n: int,
    row: Row,
    ctx: Ctx,
    op: Operator,
    head: str,
    out: Callable[[str], None],
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[str, str | None, list[Check]]:
    """("pass" | "fail" | "skipped", note, checks) for one row."""
    if not row.check:
        choice, note = await _wait(
            op,
            await op.ask(
                head, row.ask or "Does it hold?", ["Pass", "Fail", "Skip"], "Fail"
            ),
            sleep,
        )
        return {"Pass": "pass", "Fail": "fail"}.get(choice, "skipped"), note, []
    while True:
        handle = (
            await op.ask(head, row.ask or "Checking…", ["Skip"]) if row.ask else None
        )
        deadline = time.monotonic() + row.wait_min * 60
        while True:
            try:
                checks = await row.check(ctx)
            except (WardenError, httpx.HTTPError) as e:
                checks = [Check("the warden answered", False, str(e))]
            if all(c.ok for c in checks):
                await op.tell(head, "passed")
                return "pass", None, checks
            if handle and await op.answer(handle):
                return "skipped", None, checks
            if time.monotonic() >= deadline:
                break
            await sleep(POLL_S)
        failing = "; ".join(
            f"{c.label}" + (f" ({c.detail})" if c.detail else "")
            for c in checks
            if not c.ok
        )
        out(f"  checks failing: {failing}")
        choice, note = await _wait(
            op,
            await op.ask(
                head, f"Not passing: {failing[:300]}", ["Retry", "Fail", "Skip"], "Fail"
            ),
            sleep,
        )
        if choice == "Fail":
            return "fail", note, checks
        if choice == "Skip":
            return "skipped", None, checks


def signoff_file(
    cfg: config.Config, results: Results, printer: str, done: dict[str, list[int]]
) -> Path:
    """`<storage>/signoff-<printer>-<date>.md`: the sign-off table and what failed, ready to paste or file."""
    path = cfg.storage_dir / f"signoff-{printer}-{time.strftime('%Y%m%d')}.md"
    failed = [
        f"- Row {n} ({TITLES[n]}): {e.get('note') or 'checks failed'}"
        + "".join(
            f"\n  - {c['label']}: {c['detail']}"
            for c in e.get("checks") or []
            if not c["ok"]
        )
        for n in done["failed"]
        if (e := results.get(n, printer))
    ]
    path.write_text(
        f"# Live validation: {printer}, {time.strftime('%Y-%m-%d')}\n\n"
        f"Signed {len(done['signed'])}, failed {len(done['failed'])}, skipped {len(done['skipped'])}, "
        f"already settled {len(done['already'])}.\n\n"
        + ("## Failed\n\n" + "\n".join(failed) + "\n\n" if failed else "")
        + "## Sign-off\n\n"
        + report(cfg, results)
        + "\n"
    )
    return path


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="print-warden validate", description=__doc__.split("\n\n")[0]
    )
    ap.add_argument(
        "what",
        nargs="?",
        help="a row number, `session`, `sign` or `report` (none: list the rows)",
    )
    ap.add_argument("row", nargs="?", type=int, help="with `sign`: the row")
    ap.add_argument(
        "--config", default=os.environ.get("PRINT_WARDEN_CONFIG", "printers.yaml")
    )
    ap.add_argument("--printer", help="the printer (needed with more than one)")
    ap.add_argument(
        "--setup",
        action="store_true",
        help="set up what the row needs first (asks y/N)",
    )
    ap.add_argument("--yes", action="store_true", help="do not ask before a setup")
    ap.add_argument(
        "--wait", type=float, help="minutes to keep checking (default: the row's)"
    )
    ap.add_argument("--by", help="initials: a passing run, or `sign`, signs the row")
    ap.add_argument(
        "--issue", help="with `sign`: the issue a failing row is tracked in"
    )
    ap.add_argument(
        "--model",
        help="row 35: a multi-plate 3MF already uploaded, instead of four cubes",
    )
    ap.add_argument("--url", help="the warden (default http://127.0.0.1:<port>)")
    ap.add_argument(
        "--rows", help="with `session`: which rows, e.g. 1-13,39 (default: all)"
    )
    ap.add_argument(
        "--terminal",
        action="store_true",
        help="with `session`: ask here instead of on the phone",
    )
    return ap


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    cfg = config.load(args.config)
    results = Results.load(cfg.storage_dir / "validation.json")
    if args.what is None:
        print(listing(cfg, results))
        return 0
    if args.what == "report":
        print(report(cfg, results))
        return 0
    printer = args.printer or (
        next(iter(cfg.printers)) if len(cfg.printers) == 1 else None
    )
    if printer is not None and printer not in cfg.printers:
        print(f"unknown printer {printer}; one of {', '.join(cfg.printers)}")
        return 2
    if args.what == "sign":
        if args.row not in TITLES or not printer or not (args.by or args.issue):
            print(
                "usage: print-warden validate sign <row> --printer P (--by <initials> | --issue <url>)"
            )
            return 2
        entry = {"result": "signed" if args.by and not args.issue else "fail"}
        entry |= {k: v for k, v in (("by", args.by), ("issue", args.issue)) if v}
        results.record(args.row, printer, entry)
        results.save()
        print(f"Row {args.row} on {printer}: {cell(results.get(args.row, printer))}")
        return 0
    if args.what == "session":
        if not printer or not args.by:
            print(
                "usage: print-warden validate session --printer P --by <initials> [--rows 1-13] [--terminal]"
            )
            return 2
        return _run_session(cfg, results, printer, args)
    try:
        n = int(args.what)
    except ValueError:
        n = -1
    if n not in TITLES:
        print(f"no row {args.what}: rows are 0-{max(TITLES)}, `sign` or `report`")
        return 2
    row = ROWS[n]
    if not printer and not row.every_printer:
        print(f"--printer: one of {', '.join(cfg.printers)}")
        return 2
    printer = printer or next(iter(cfg.printers))
    url = args.url or f"http://127.0.0.1:{cfg.port}"

    async def go() -> int:
        async with McpWarden(
            url, os.environ.get("PRINT_WARDEN_TOKEN") or cfg.api_token
        ) as w:
            ctx = Ctx(
                cfg,
                w,
                printer,
                results.scratch(n, printer),
                results,
                args.by,
                args.model,
            )
            return await run_row(
                n, ctx, args.setup, args.wait, (lambda _: True) if args.yes else _yes
            )

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        print("\nStopped; run it again to check.")
        return 1
    except (httpx.HTTPError, OSError, ExceptionGroup) as e:
        while isinstance(e, ExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        print(f"cannot reach the warden at {url}: {e}")
        return 1


def _run_session(
    cfg: config.Config, results: Results, printer: str, args: argparse.Namespace
) -> int:
    url = args.url or f"http://127.0.0.1:{cfg.port}"

    async def go() -> int:
        async with McpWarden(
            url, os.environ.get("PRINT_WARDEN_TOKEN") or cfg.api_token
        ) as w:
            op: Operator = (
                TerminalOperator() if args.terminal else PhoneOperator(w, printer)
            )

            def make(n: int) -> Ctx:
                return Ctx(
                    cfg,
                    w,
                    printer,
                    results.scratch(n, printer),
                    results,
                    args.by,
                    args.model,
                )

            done = await session(_rows(args.rows), make, op, args.by)
            path = signoff_file(cfg, results, printer, done)
            print(
                f"\nSigned {len(done['signed'])}, failed {len(done['failed'])}, skipped {len(done['skipped'])}. "
                f"Sign-off: {path}"
            )
            return 1 if done["failed"] else 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        print("\nStopped; `validate session` again resumes where it left off.")
        return 1
    except (httpx.HTTPError, OSError, ExceptionGroup) as e:
        while isinstance(e, ExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        print(f"cannot reach the warden at {url}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
