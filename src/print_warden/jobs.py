"""Job ledger: one SQLite table, explicit state machine, every transition logged with who authorized it.
Transitions outside TRANSITIONS raise; a log entry that changes nothing goes through note()."""

from __future__ import annotations

import builtins
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

STATES = (
    "sliced",
    "staged",
    "awaiting_approval",
    "approved",
    "printing",
    "done",
    "failed",
    "skipped",
)
# A print started at the printer (Mainsail, the screen) is a human start, so staged jobs may go straight to printing.
TRANSITIONS: dict[str, frozenset[str]] = {
    "sliced": frozenset({"staged", "failed", "skipped"}),
    "staged": frozenset({"awaiting_approval", "approved", "printing", "skipped"}),
    "awaiting_approval": frozenset({"approved", "staged", "printing", "skipped"}),
    "approved": frozenset({"staged", "printing", "skipped"}),
    "printing": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
}
FIELDS = (
    "id",
    "created",
    "updated",
    "printer",
    "name",
    "material",
    "quality",
    "state",
    "stl",
    "gcode",
    "preview",
    "remote_name",
    "est_seconds",
    "filament_g",
    "layers",
    "max_z",
    "warnings",
    "authorized_by",
    "started_at",
    "finished_at",
    "error",
    "log",
    "spool_id",
    "filament_m",
    "filament_used_mm",
    "approval_requested_at",
    "timelapse",
    "slice_blockers",
    "queue_pos",
    "offer",
    "copies",
    "model_hash",
    "overrides",
    "orient",
    "print_duration_s",
    "grams_used",
    "cost",
    "outcome",
    "outcome_reason",
    "outcome_by",
    "outcome_at",
    "outcome_hint",
)
JSON_FIELDS = ("warnings", "slice_blockers", "overrides")
ADDED_COLUMNS = {
    "spool_id": "integer",
    "filament_m": "real",
    "filament_used_mm": "real",
    "approval_requested_at": "real",
    "timelapse": "text",
    "slice_blockers": "text",
    "queue_pos": "real",
    "offer": "integer",
    "copies": "integer",
    "model_hash": "text",
    "overrides": "text",
    "orient": "integer",
    "print_duration_s": "real",
    "grams_used": "real",
    "cost": "real",
    "outcome": "text",
    "outcome_reason": "text",
    "outcome_by": "text",
    "outcome_at": "real",
    "outcome_hint": "text",
}


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "create table if not exists jobs (id text primary key, created real, updated real, printer text, name text,"
            " material text, quality text, state text, stl text, gcode text, preview text, remote_name text,"
            " est_seconds integer, filament_g real, layers integer, max_z real, warnings text, authorized_by text,"
            " started_at real, finished_at real, error text, log text)"
        )
        have = {r[1] for r in self._db.execute("pragma table_info(jobs)")}
        for col, typ in ADDED_COLUMNS.items():
            if col not in have:
                self._db.execute(f"alter table jobs add column {col} {typ}")
        self._db.execute("create table if not exists kv (k text primary key, v text)")
        self._db.commit()

    def create(self, **kw: Any) -> dict[str, Any]:
        now = time.time()
        row: dict[str, Any] = {f: None for f in FIELDS}
        row.update(
            {
                "id": uuid.uuid4().hex[:10],
                "created": now,
                "updated": now,
                "state": "sliced",
                "log": "[]",
            }
        )
        row.update(kw)
        row["queue_pos"] = row["queue_pos"] if row["queue_pos"] is not None else now
        for k in JSON_FIELDS:
            if isinstance(row.get(k), list | dict):
                row[k] = json.dumps(row[k])
        self._db.execute(
            f"insert into jobs ({','.join(FIELDS)}) values ({','.join('?' * len(FIELDS))})",
            [row[f] for f in FIELDS],
        )
        self._db.commit()
        return self.get(row["id"])

    def get(self, job_id: str) -> dict[str, Any]:
        cur = self._db.execute(
            f"select {','.join(FIELDS)} from jobs where id=?", (job_id,)
        )
        r = cur.fetchone()
        if not r:
            raise KeyError(f"no job {job_id}")
        d = dict(zip(FIELDS, r, strict=True))
        for k in JSON_FIELDS:
            d[k] = json.loads(d[k] or ("{}" if k == "overrides" else "[]"))
        d["offer"] = bool(d["offer"])
        d["log"] = json.loads(d["log"] or "[]")
        return d

    def list(
        self,
        printer: str | None = None,
        limit: int = 20,
        states: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        q, args = "select id from jobs where 1=1", list[Any]()
        if printer:
            q += " and printer=?"
            args.append(printer)
        if states:
            q += f" and state in ({','.join('?' * len(states))})"
            args += list(states)
        q += " order by created desc limit ?"
        args.append(limit)
        return [self.get(r[0]) for r in self._db.execute(q, args)]

    def transition(
        self, job_id: str, state: str, note: str = "", **fields: Any
    ) -> dict[str, Any]:
        if state not in STATES:
            raise ValueError(state)
        job = self.get(job_id)
        if state not in TRANSITIONS[job["state"]]:
            raise ValueError(f"job {job_id} is {job['state']}; cannot move to {state}")
        return self._write(job, state, note, fields)

    def note(self, job_id: str, note: str, **fields: Any) -> dict[str, Any]:
        """Append to the log (and set fields) without changing state."""
        job = self.get(job_id)
        return self._write(job, job["state"], note, fields)

    def _write(
        self, job: dict[str, Any], state: str, note: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown job fields {sorted(unknown)}")
        now = time.time()
        log = job["log"] + [
            {"ts": now, "from": job["state"], "to": state, "note": note}
        ]
        fields = {
            k: json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()
        }
        sets = {"state": state, "updated": now, "log": json.dumps(log), **fields}
        self._db.execute(
            f"update jobs set {','.join(f'{k}=?' for k in sets)} where id=?",
            [*sets.values(), job["id"]],
        )
        self._db.commit()
        return self.get(job["id"])

    def queue(self, printer: str) -> builtins.list[dict[str, Any]]:
        """Staged jobs on a printer, in the order they will be offered."""
        ids = self._db.execute(
            "select id from jobs where printer=? and state='staged' order by queue_pos, created",
            (printer,),
        )
        return [self.get(r[0]) for r in ids]

    def by_model(
        self, model_hash: str, limit: int = 20
    ) -> builtins.list[dict[str, Any]]:
        ids = self._db.execute(
            "select id from jobs where model_hash=? order by created desc limit ?",
            (model_hash, limit),
        )
        return [self.get(r[0]) for r in ids]

    def since(
        self, ts: float, printer: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Jobs created since `ts`, oldest first."""
        q, args = "select id from jobs where created>=?", list[Any]([ts])
        if printer:
            q += " and printer=?"
            args.append(printer)
        return [self.get(r[0]) for r in self._db.execute(q + " order by created", args)]

    def find_active(self, printer: str) -> dict[str, Any] | None:
        rows = self.list(printer, 1, ("printing",))
        return rows[0] if rows else None

    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self._db.execute("select v from kv where k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self._db.execute(
            "insert into kv (k, v) values (?,?) on conflict(k) do update set v=excluded.v",
            (k, v),
        )
        self._db.commit()
