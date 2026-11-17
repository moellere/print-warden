"""The live-validation rows that check themselves, run end to end against `print-warden demo`.

One demo (the real warden, simulated printers, phone and slicer) in a subprocess; each row goes through `validate`'s
own setup and checks, and the operator's taps go through the simulated phone.
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from print_warden import config, validate

ROWS_COVERED = (0, 1, 3, 4, 10, 11, 12, 13, 23, 25, 28, 35, 38, 39, 41)


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.contextmanager
def running_demo(root, *extra):
    """A demo in a subprocess (its log in `root/demo.log`); yields (root, port, sim_port)."""
    port, sim_port = _port(), _port()
    env = {**os.environ, "PRINT_WARDEN_TOKEN": "demo"}
    log = (root / "demo.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "print_warden.demo", "--dir", str(root), "--port", str(port),
            "--sim-port", str(sim_port), "--speed", "120", "--pause-cancel-minutes", "0.05", *extra,
        ],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).json()[
                    "ok"
                ]:
                    break
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            time.sleep(0.3)
        else:
            pytest.fail(
                "demo did not start:\n" + (root / "demo.log").read_text(errors="ignore")
            )
        yield root, port, sim_port
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    with running_demo(tmp_path_factory.mktemp("demo")) as d:
        yield d


class Phone:
    def __init__(self, sim_port: int):
        self.base = f"http://127.0.0.1:{sim_port}"
        self.c = httpx.AsyncClient(timeout=10, headers={"accept": "application/json"})

    async def state(self):
        return (await self.c.get(f"{self.base}/sim/notifications")).json()

    async def action(self, prefix: str, wait_s: float = 20) -> str:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for n in (await self.state())["notifications"]:
                for a in n["actions"]:
                    if a["action"].startswith(prefix):
                        return a["action"]
            await asyncio.sleep(0.2)
        raise AssertionError(f"no {prefix} button on the phone: {await self.state()}")

    async def tap(self, prefix: str, wait_s: float = 20) -> None:
        action = await self.action(prefix, wait_s)
        await self.c.post(f"{self.base}/sim/tap", data={"action": action})

    async def take_part_off(self, printer: str) -> None:
        await self.c.post(f"{self.base}/sim/bed", data={"printer": printer})

    async def console(self, printer: str, script: str) -> None:
        await self.c.post(
            f"{self.base}/sim/gcode", data={"printer": printer, "script": script}
        )


async def _until(pred, wait_s: float = 30):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if out := await pred():
            return out
        await asyncio.sleep(0.2)
    raise AssertionError("timed out")


async def test_the_runbook_against_the_demo(demo, monkeypatch):
    root, port, sim_port = demo
    monkeypatch.setenv("PRINT_WARDEN_TOKEN", "demo")
    monkeypatch.setattr(validate, "POLL_S", 0.3)
    cfg = config.load(root / "printers.yaml")
    results = validate.Results.load(root / "data" / "validation.json")
    phone = Phone(sim_port)
    failed: dict[int, str] = {}

    async with validate.McpWarden(f"http://127.0.0.1:{port}", "demo") as w:

        async def row(
            n: int, printer: str = "demo-a", setup: bool = False, wait: float = 0.5
        ) -> None:
            lines: list[str] = []
            ctx = validate.Ctx(
                cfg, w, printer, results.scratch(n, printer), results, by="CI"
            )
            if await validate.run_row(
                n, ctx, setup, wait, lambda _: True, lines.append
            ):
                failed[n] = "\n".join(lines)
            else:
                failed.pop(n, None)

        async def job(job_id: str):
            return await w.call("job_status", job_id=job_id)

        async def clear_bed(printer: str = "demo-a") -> None:
            await phone.take_part_off(printer)
            await w.call(
                "mark_bed_clear",
                printer=printer,
                authorized_by="CI: the simulated part was taken off",
            )

        async def print_cube(printer: str = "demo-a") -> str:
            ctx = validate.Ctx(cfg, w, printer, {}, results)
            staged = await validate._cube_job(ctx)
            await w.call("request_start", job_id=staged["id"])
            await phone.tap(f"PRINT_WARDEN_START_{staged['id']}")
            await _until(lambda: _state_in(staged["id"], ("printing", "done")))
            return str(staged["id"])

        async def _state_in(job_id: str, states):
            return (await job(job_id))["state"] in states

        await row(0)
        await clear_bed()  # the first Bed cleared takes the empty-bed reference
        await row(1, setup=True)
        await row(3)
        await row(4, setup=True, wait=0)
        await phone.tap("PRINT_WARDEN_START_")
        await row(4, wait=0.5)
        first = results.scratch(4, "demo-a")["job_id"]
        await _until(lambda: _state_in(first, ("done",)))
        await row(1, setup=True)
        await row(10)
        await row(12)
        queued = await w.call(
            "submit_job",
            printer="demo-a",
            model=validate.CUBE,
            material="PLA",
            quality="draft",
            offer=True,
        )
        await phone.take_part_off("demo-a")
        await phone.tap("PRINT_WARDEN_BEDCLEAR_demo-a")
        await row(11)
        await w.call("skip_job", job_id=queued["id"], reason="CI")

        cancel = await print_cube()
        await phone.tap(f"PRINT_WARDEN_CANCEL_{cancel}")
        await row(13)

        await clear_bed()
        await print_cube()
        await row(38, setup=True, wait=1)

        await clear_bed()
        await row(39, setup=True, wait=0)
        fault = results.scratch(39, "demo-a")["job_id"]
        await phone.tap(f"PRINT_WARDEN_START_{fault}")
        await phone.tap(f"PRINT_WARDEN_REPRINT_{fault}")
        await row(39, wait=1)
        reprint = next(
            j
            for j in await w.call("list_jobs", printer="demo-a")
            if j.get("reprint_of") == fault
        )
        await w.call("skip_job", job_id=reprint["id"], reason="CI")

        await phone.console("demo-b", "M112")
        await row(25, printer="demo-b")
        await phone.console("demo-b", "FIRMWARE_RESTART")

        await row(28)
        await row(41, printer="demo-b", setup=True, wait=0)
        await phone.console(
            "demo-b", "SET_SIM_CONFIG SECTION=printer KEY=max_accel VALUE=5000"
        )
        await row(41, printer="demo-b", wait=0.5)

        await clear_bed("demo-a")
        await clear_bed("demo-b")
        await row(23, setup=True, wait=0)
        for jid in results.scratch(23, "demo-a")["job_ids"]:
            await phone.tap(f"PRINT_WARDEN_START_{jid}")
        await row(23, wait=0.5)

        await row(35, setup=True, wait=0.5)

        # the guided session asks on the (simulated) phone and reads the tap back
        op = validate.PhoneOperator(w, "demo-b")
        sess = asyncio.create_task(
            validate.session(
                [9],
                lambda n: validate.Ctx(
                    cfg, w, "demo-b", results.scratch(n, "demo-b"), results, by="CI"
                ),
                op,
                "CI",
                out=lambda _: None,
            )
        )
        await phone.tap("PRINT_WARDEN_VALIDATE_", wait_s=20)
        done = await asyncio.wait_for(sess, 20)
        assert done["signed"] == [9]
        assert results.get(9, "demo-b")["by"] == "CI"

    assert not failed, "\n\n".join(f"row {n}:\n{out}" for n, out in failed.items())
    assert {
        n
        for n in ROWS_COVERED
        if (results.get(n, "demo-a") or results.get(n, "demo-b"))
    } == set(ROWS_COVERED)
