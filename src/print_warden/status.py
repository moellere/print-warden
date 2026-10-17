"""`/status`: one read-only page for the whole warden, behind the same token as `/mcp`.

Per printer: state, progress and finish time, bed, the queue with estimates, the latest snapshot and maintenance
that is due; then filament that needs reordering or drying. It refreshes itself every 30 s. Nothing on the page
acts: starts, cancels and bed-clears stay on the phone. Every value is escaped. Snapshots load from `/files`, which
is open (as for notification images), so the token never lands in an image URL.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import quote

CSS = """
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e2dd;--ok:#2f7d4f;--warn:#b26b00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#21211f;--ink:#ecece8;--mute:#9a9a94;--line:#33332f}}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 16px}h2{font-size:16px;margin:0 0 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.mute{color:var(--mute)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
img{width:100%;border-radius:6px;margin-top:8px}table{width:100%;border-collapse:collapse}
td{padding:3px 0;border-top:1px solid var(--line);vertical-align:top}td.r{text-align:right;white-space:nowrap}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;background:var(--ok)}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _dur(s: float | None) -> str:
    if s is None:
        return "–"
    m = int(s // 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _img(path: str | None) -> str:
    if not path:
        return ""
    return f'<img src="{_e("/files/" + quote(path))}" alt="latest snapshot" loading="lazy">'


def _state_class(p: dict[str, Any]) -> str:
    if not p.get("connected"):
        return "bad"
    if p.get("klippy") not in ("ready", None) or p.get("last_shutdown"):
        return "bad"
    return "ok" if p.get("state") in ("printing", "paused") else ""


def render(data: dict[str, Any], now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    cards = []
    for name, p in sorted(data["printers"].items()):
        state = (
            (p.get("state") or p.get("klippy") or "unknown")
            if p.get("connected")
            else "offline"
        )
        rows = []
        if p.get("last_shutdown"):
            sd = p["last_shutdown"]
            rows.append(
                f'<p class="bad">Klipper shut down: {_e(str(sd["cause"]).replace("_", " "))} — {_e(sd["hint"])}</p>'
            )
        prog = p.get("progress_pct")
        if prog is not None:
            rows.append(
                f'<div class="bar"><i style="width:{max(0.0, min(100.0, float(prog))):.0f}%"></i></div>'
                f"<p>{float(prog):.0f}% · {_e(p.get('job') or '')} · finishes in {_dur(p.get('finishes_in_s'))}</p>"
            )
        rows.append(
            f'<p class="mute">Bed {_e(p.get("bed") or "unknown")} · {_e(p.get("material") or "no material set")}</p>'
        )
        queue = p.get("queue_jobs") or []
        if queue:
            rows.append(
                "<table>"
                + "".join(
                    f'<tr><td>{_e(j["name"])} <span class="mute">{_e(j.get("material"))}</span></td>'
                    f'<td class="r">{_dur(j.get("est_seconds"))}</td></tr>'
                    for j in queue
                )
                + "</table>"
            )
        else:
            rows.append('<p class="mute">Queue empty</p>')
        due = p.get("maintenance_due") or []
        if due:
            rows.append(f'<p class="warn">Maintenance due: {_e(", ".join(due))}</p>')
        rows.append(_img(p.get("snapshot")))
        cards.append(
            f'<section class="card"><h2>{_e(name)} <span class="{_state_class(p)}">{_e(state)}</span></h2>'
            + "".join(rows)
            + "</section>"
        )
    upkeep = []
    for r in data.get("reorder") or []:
        upkeep.append(
            f'<p class="warn">{_e(r["material"])}: {_e(r["after_queue_g"])} g left after the queue</p>'
        )
    for d in data.get("drying") or []:
        upkeep.append(
            f'<p class="warn">Dry the {_e(d["material"])} on {_e(d["printer"])} ({_e(d["days_open"])} days open)</p>'
        )
    if upkeep:
        cards.append(
            '<section class="card"><h2>Filament</h2>' + "".join(upkeep) + "</section>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="30"><title>print-warden</title>'
        f"<style>{CSS}</style></head><body><main>"
        f'<h1>print-warden <span class="mute">{_e(data.get("version"))} · {now:%H:%M}</span></h1>'
        f'<div class="grid">{"".join(cards)}</div></main></body></html>'
    )
