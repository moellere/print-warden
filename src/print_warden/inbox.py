"""`/inbox`: every button prompt the warden has sent that is still waiting, with the phone's buttons, in a browser.

Each actionable notification is recorded here as it is sent (whether or not the phone got it), keyed by its
notification tag, so a newer prompt with the same tag replaces the old one. It leaves when the warden withdraws
the notification, when it is answered on either side, or after `MAX_AGE_S`. Swiping a notification away tells
the warden nothing, so the prompt stays here. A button posts its action to the same handler a phone tap reaches.
The page needs the operator's sign-in (`signin.py`), never the API token alone.

Every answer, from the phone or here, is kept for `RECENT_S` and listed under Recent: what was asked, the button,
where it came from and when. The server says which answers can still be undone (`undo_ok`) and why not otherwise.
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Sequence
from typing import Any, Protocol
from urllib.parse import quote

KEY = "prompt:"
ANSWER = "answer:"
MAX_AGE_S = 7 * 86400
RECENT_S = 86400
RECENT_MAX = 20


class Store(Protocol):
    def kv_get(self, k: str, default: str | None = None) -> str | None: ...
    def kv_set(self, k: str, v: str) -> None: ...
    def kv_delete(self, k: str) -> bool: ...
    def kv_prefix(self, prefix: str) -> dict[str, str]: ...


def record(
    store: Store,
    tag: str,
    title: str,
    message: str,
    actions: Sequence[tuple[str, ...]],
    image: str | None = None,
    now: float | None = None,
) -> None:
    store.kv_set(
        KEY + tag,
        json.dumps(
            {
                "tag": tag,
                "title": title,
                "message": message,
                "actions": [list(a) for a in actions],
                "image": image,
                "at": time.time() if now is None else now,
            }
        ),
    )


def clear(store: Store, tag: str) -> None:
    store.kv_delete(KEY + tag)


def open_prompts(store: Store, now: float | None = None) -> list[dict[str, Any]]:
    """Waiting prompts, newest first; those older than `MAX_AGE_S` are dropped."""
    t = time.time() if now is None else now
    out = []
    for k, v in store.kv_prefix(KEY).items():
        p = json.loads(v)
        if p.get("at", 0) < t - MAX_AGE_S:
            store.kv_delete(k)
            continue
        out.append(p)
    return sorted(out, key=lambda p: -p["at"])


def answered(store: Store, action: str) -> list[dict[str, Any]]:
    """Remove every prompt that offered `action` and return them (their tags withdraw the notifications)."""
    out = []
    for k, v in store.kv_prefix(KEY).items():
        p = json.loads(v)
        if any(a[0] == action for a in p.get("actions") or []):
            store.kv_delete(k)
            out.append(p)
    return out


def remember(store: Store, entry: dict[str, Any]) -> dict[str, Any]:
    """Keep one answer for Recent; `entry` has at least `at` and `action`. Returns it with its `id`."""
    entry = {**entry, "id": f"{entry['at']:.6f}"}
    store.kv_set(ANSWER + entry["id"], json.dumps(entry))
    return entry


def answer(store: Store, answer_id: str) -> dict[str, Any] | None:
    raw = store.kv_get(ANSWER + answer_id)
    return json.loads(raw) if raw else None


def update_answer(store: Store, entry: dict[str, Any]) -> None:
    store.kv_set(ANSWER + entry["id"], json.dumps(entry))


def recent(store: Store, now: float | None = None) -> list[dict[str, Any]]:
    """Answers from the last `RECENT_S`, newest first, at most `RECENT_MAX`; older ones are dropped."""
    t = time.time() if now is None else now
    out = []
    for k, v in store.kv_prefix(ANSWER).items():
        e = json.loads(v)
        if e.get("at", 0) < t - RECENT_S:
            store.kv_delete(k)
            continue
        out.append(e)
    return sorted(out, key=lambda e: -e["at"])[:RECENT_MAX]


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _ago(t: float, now: float) -> str:
    m = int((now - t) // 60)
    return (
        "just now"
        if m < 1
        else f"{m} min ago"
        if m < 60
        else f"{m // 60} h {m % 60:02d} min ago"
    )


def _buttons(actions: list[list[str]], csrf: str) -> str:
    hidden = f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
    plain: list[list[str]] = []
    typed: list[list[str]] = []
    for a in actions:
        (typed if len(a) > 2 and a[2] == "textInput" else plain).append(a)
    out = ""
    if plain:
        out += '<form class="act" method="post" action="/inbox">' + hidden
        out += "".join(
            f'<button name="action" value="{_e(a[0])}">{_e(a[1])}</button> '
            for a in plain
        )
        out += "</form>"
    for a in typed:
        out += (
            f'<form class="act" method="post" action="/inbox">{hidden}'
            f'<input type="hidden" name="action" value="{_e(a[0])}">'
            '<input name="reply" required style="font:inherit;padding:5px;max-width:240px"> '
            f"<button>{_e(a[1])}</button></form>"
        )
    return out


def _recent(answers: list[dict[str, Any]], csrf: str, now: float) -> str:
    if not answers:
        return ""
    rows = []
    for a in answers:
        what = f"{_e(a.get('choice') or a['action'])}"
        title = (
            f' <span class="mute">· {_e(a["title"])}</span>' if a.get("title") else ""
        )
        if a.get("undone_at"):
            side = f'<span class="mute">undone {_ago(a["undone_at"], now)}</span>'
        elif a.get("undo_ok"):
            side = (
                f'<form class="act" method="post" action="/inbox" style="display:inline;margin:0">'
                f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
                f'<button name="undo" value="{_e(a["id"])}">Undo</button></form>'
            )
        else:
            side = f'<span class="mute">{_e(a.get("undo_why") or "")}</span>'
        rows.append(
            f'<tr><td><b>{what}</b>{title}<br><span class="mute">{_e(a.get("via"))}, '
            f"{_ago(a['at'], now)}</span>"
            + (
                f'<br><span class="bad">{_e(a["refused"])}</span>'
                if a.get("refused")
                else ""
            )
            + f'</td><td class="r">{side}</td></tr>'
        )
    return (
        '<section class="card" style="margin-top:16px"><h2>Recent</h2><table>'
        + "".join(rows)
        + "</table></section>"
    )


def render(
    css: str,
    prompts: list[dict[str, Any]],
    csrf: str,
    now: float | None = None,
    message: str | None = None,
    error: str | None = None,
    answers: list[dict[str, Any]] | None = None,
) -> str:
    t = time.time() if now is None else now
    head = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><meta http-equiv="refresh" content="60"><title>Inbox · print warden</title>'
        f"<style>{css}form.act button{{margin:2px 0}}</style></head><body><main>"
        '<h1>Inbox <span class="mute">· <a href="/status">status</a></span></h1>'
    )
    notes = (f'<p class="ok">{_e(message)}</p>' if message else "") + (
        f'<p class="bad">{_e(error)}</p>' if error else ""
    )
    if not prompts:
        cards = '<div class="card mute">Nothing is waiting for you.</div>'
    else:
        cards = (
            '<div class="grid">'
            + "".join(
                '<div class="card">'
                f"<h2>{_e(p['title'])}</h2><p>{_e(p['message'])}</p>"
                + (f'<p class="warn">{_e(p["note"])}</p>' if p.get("note") else "")
                + (
                    f'<img src="{_e(p["image"])}" alt="preview" loading="lazy">'
                    if p.get("image")
                    else ""
                )
                + _buttons(p.get("actions") or [], csrf)
                + f'<p class="mute">sent {_ago(p["at"], t)}</p></div>'
                for p in prompts
            )
            + "</div>"
        )
    foot = (
        '<form method="post" action="/signout" style="margin-top:16px">'
        f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        '<button style="font:inherit">Sign out</button></form>'
    )
    return (
        head
        + notes
        + cards
        + _recent(answers or [], csrf, t)
        + foot
        + "</main></body></html>"
    )


def link(count: int) -> str:
    """The `/status` page's link here."""
    return f'<a href="{quote("/inbox")}">Inbox ({count})</a>'
