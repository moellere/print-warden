"""`/stage`: add a print from a browser, behind the same token as `/mcp`. It slices and queues; it never starts.

The form takes a model (a file, or a URL as `submit_job` accepts one), where to print, material, quality, copies and
the 3MF plate. Posting it slices and stages the job without queueing it, then shows the preview, estimate, warnings
and blockers with two buttons: **Add to queue** (the ordinary offer: a phone tap, or `auto_start`) and **Discard**.
Both act only on a job staged from this page. Nothing here starts, cancels or waives anything.

Every form carries a CSRF token, an HMAC of the hour under the API token (or a per-process secret when the warden
runs open); a post must also come from the page's own origin when the browser says where it came from.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import time
from typing import Any
from urllib.parse import quote

from .status import CSS

_PROCESS_SECRET = os.urandom(32)
EXTRA_CSS = """
form{display:grid;gap:10px}label{display:grid;gap:4px;font-size:14px}
input,select,button{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
button{cursor:pointer;background:var(--ok);color:#fff;border:0}button.alt{background:var(--line);color:var(--ink)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1 1 140px}
"""


def _key(api_token: str | None) -> bytes:
    return (api_token or "").encode() or _PROCESS_SECRET


def csrf_token(api_token: str | None, now: float | None = None) -> str:
    hour = int((now or time.time()) // 3600)
    return hmac.new(
        _key(api_token), f"stage:{hour}".encode(), hashlib.sha256
    ).hexdigest()


def csrf_ok(given: str, api_token: str | None, now: float | None = None) -> bool:
    """Valid for the hour it was issued in and the next."""
    t = now or time.time()
    return any(
        hmac.compare_digest(given.encode(), csrf_token(api_token, t - d).encode())
        for d in (0, 3600)
    )


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _action(path: str, token: str | None) -> str:
    return _e(path + (f"?token={quote(token)}" if token else ""))


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}{EXTRA_CSS}</style></head><body><main>"
        f"<h1>{_e(title)}</h1>{body}</main></body></html>"
    )


def _options(values: list[str], chosen: str | None) -> str:
    return "".join(
        f'<option value="{_e(v)}"{" selected" if v == chosen else ""}>{_e(v)}</option>'
        for v in values
    )


def form(
    choices: dict[str, list[str]],
    csrf: str,
    token: str | None,
    error: str | None = None,
    last: dict[str, str] | None = None,
) -> str:
    """The add-a-print form. `choices` has "where", "materials" and "qualities"."""
    last = last or {}
    err = f'<p class="bad">{_e(error)}</p>' if error else ""
    body = f"""<section class="card">{err}
<form method="post" action="{_action("/stage", token)}" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{_e(csrf)}">
<label>Model file (STL, 3MF or SCAD)<input type="file" name="file" accept=".stl,.3mf,.scad"></label>
<label>or a link (a file, or a Thingiverse / Printables / other model page)
<input type="url" name="url" value="{_e(last.get("url"))}" placeholder="https://"></label>
<label>File on that page (when it has several)<input name="page_file" value="{_e(last.get("page_file"))}"></label>
<div class="row">
<label>Printer or place<select name="where">{_options(choices["where"], last.get("where"))}</select></label>
<label>Material<select name="material">{_options(choices["materials"], last.get("material"))}</select></label>
<label>Quality<select name="quality">{_options(choices["qualities"], last.get("quality") or "standard")}</select></label>
</div>
<div class="row">
<label>Copies<input type="number" name="copies" min="1" max="50" value="{_e(last.get("copies") or 1)}"></label>
<label>3MF plate<input type="number" name="plate" min="1" value="{_e(last.get("plate") or 1)}"></label>
</div>
<button type="submit">Slice</button>
<p class="mute">Slicing takes up to a minute. Nothing prints from this page: a queued job is offered on the phone.</p>
</form></section>"""
    return _page("Add a print", body)


def sliced(job: dict[str, Any], csrf: str, token: str | None) -> str:
    """The staged job with Add to queue / Discard."""
    est = job.get("est_seconds")
    mins = f"{int(est) // 3600}h {int(est) % 3600 // 60:02d}m" if est else "–"
    img = (
        f'<img src="{_e(job["preview_url"])}" alt="preview">'
        if job.get("preview_url")
        else ""
    )
    warn = "".join(f'<p class="warn">{_e(w)}</p>' for w in job.get("warnings") or [])
    block = "".join(
        f'<p class="bad">Blocker: {_e(b)}</p>' for b in job.get("slice_blockers") or []
    )
    if block:
        block += '<p class="mute">The queue passes a blocked job over until the blockers are waived in chat.</p>'
    hidden = f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="job_id" value="{_e(job["id"])}">'
    body = f"""<section class="card">
<h2>{_e(job["name"])} <span class="mute">on {_e(job["printer"])}</span></h2>
<p>{_e(job["material"])} · {_e(job.get("quality"))} · {mins} · {_e(round(job.get("filament_g") or 0))} g</p>
{warn}{block}{img}
<div class="row">
<form method="post" action="{_action("/stage/queue", token)}">{hidden}<button type="submit">Add to queue</button></form>
<form method="post" action="{_action("/stage/discard", token)}">{hidden}<button class="alt" type="submit">Discard</button></form>
</div></section>"""
    return _page("Sliced", body)


def done(message: str, token: str | None) -> str:
    return _page(
        "Add a print",
        f'<section class="card"><p>{_e(message)}</p>'
        f'<p><a href="{_action("/stage", token)}">Add another</a> · '
        f'<a href="{_action("/status", token)}">Status</a></p></section>',
    )
