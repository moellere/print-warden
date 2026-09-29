"""Event store: one SQLite table, newest-first queries, optional snapshot path per event."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SEVERITIES = ("debug", "info", "warning", "critical")


@dataclass
class Event:
    printer: str
    kind: str
    severity: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    snapshot: str | None = None
    alert: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        return d


class EventStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "create table if not exists events (id integer primary key, ts real, printer text, kind text,"
            " severity text, message text, data text, snapshot text, alert integer)"
        )
        self._db.execute("create index if not exists ix_events_ts on events(ts)")
        self._db.commit()

    def add(self, ev: Event) -> int:
        cur = self._db.execute(
            "insert into events (ts, printer, kind, severity, message, data, snapshot, alert) values (?,?,?,?,?,?,?,?)",
            (
                ev.ts,
                ev.printer,
                ev.kind,
                ev.severity,
                ev.message,
                json.dumps(ev.data),
                ev.snapshot,
                int(ev.alert),
            ),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def query(
        self,
        printer: str | None = None,
        since: float | None = None,
        limit: int = 50,
        min_severity: str = "info",
    ) -> list[dict[str, Any]]:
        sevs = SEVERITIES[SEVERITIES.index(min_severity) :]
        q = f"select ts, printer, kind, severity, message, data, snapshot, alert from events where severity in ({','.join('?' * len(sevs))})"
        args: list[Any] = list(sevs)
        if printer:
            q += " and printer=?"
            args.append(printer)
        if since:
            q += " and ts>=?"
            args.append(since)
        q += " order by ts desc limit ?"
        args.append(limit)
        out = []
        for ts, pr, kind, sev, msg, data, snap, alert in self._db.execute(q, args):
            out.append(
                Event(
                    pr, kind, sev, msg, json.loads(data or "{}"), ts, snap, bool(alert)
                ).as_dict()
            )
        return out

    def prune(self, keep_days: int) -> int:
        cur = self._db.execute(
            "delete from events where ts < ?", (time.time() - keep_days * 86400,)
        )
        self._db.commit()
        return cur.rowcount
