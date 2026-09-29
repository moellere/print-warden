"""Job ledger: one SQLite table, explicit state machine, every transition logged with who authorized it."""

from __future__ import annotations

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
)


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
        self._db.execute("create table if not exists kv (k text primary key, v text)")
        self._db.commit()

    def create(self, **kw: Any) -> dict[str, Any]:
        now = time.time()
        row = {f: None for f in FIELDS}
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
        if isinstance(row.get("warnings"), list):
            row["warnings"] = json.dumps(row["warnings"])
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
        d["warnings"] = json.loads(d["warnings"] or "[]")
        d["log"] = json.loads(d["log"] or "[]")
        return d

    def list(
        self,
        printer: str | None = None,
        limit: int = 20,
        states: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        q, args = "select id from jobs where 1=1", []
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
        log = job["log"] + [
            {"ts": time.time(), "from": job["state"], "to": state, "note": note}
        ]
        sets = {
            "state": state,
            "updated": time.time(),
            "log": json.dumps(log),
            **fields,
        }
        self._db.execute(
            f"update jobs set {','.join(f'{k}=?' for k in sets)} where id=?",
            [*sets.values(), job_id],
        )
        self._db.commit()
        return self.get(job_id)

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
