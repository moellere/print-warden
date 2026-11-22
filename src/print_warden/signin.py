"""Signing in as the operator, for the pages a person uses: `/status`, `/stage`, `/spools` and `/inbox`.

The API token (`PRINT_WARDEN_TOKEN`) is for machines, and every MCP client holds it. So it cannot be what lets a
browser press Start in the inbox: an agent with the token could post that form itself. The operator password
(`PRINT_WARDEN_OPERATOR_PASSWORD`, set only in the warden's environment and never given to an MCP client) is.
A browser signs in once and keeps a cookie for `DAYS`: `<expiry>.<HMAC of the expiry under the password>`, so
changing the password signs every browser out. `Limiter` refuses sign-in for `LOCK_S` after `MAX_FAILS` wrong
passwords within that time.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import time
from collections import deque

COOKIE = "pw_operator"
DAYS = 30
MAX_FAILS = 5
LOCK_S = 600


def _sig(password: str, exp: int) -> str:
    key = hashlib.sha256(b"print-warden operator:" + password.encode()).digest()
    return hmac.new(key, f"operator:{exp}".encode(), hashlib.sha256).hexdigest()


def cookie_value(password: str, now: float | None = None, days: float = DAYS) -> str:
    exp = int((time.time() if now is None else now) + days * 86400)
    return f"{exp}.{_sig(password, exp)}"


def cookie_ok(
    value: str | None, password: str | None, now: float | None = None
) -> bool:
    if not (value and password):
        return False
    exp_s, _, sig = value.partition(".")
    if not exp_s.isdigit() or int(exp_s) < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(sig.encode(), _sig(password, int(exp_s)).encode())


def password_ok(given: str, password: str | None) -> bool:
    return bool(password) and hmac.compare_digest(
        hashlib.sha256(given.encode()).digest(),
        hashlib.sha256((password or "").encode()).digest(),
    )


class Limiter:
    """Wrong passwords in the last `LOCK_S`; at `MAX_FAILS` sign-in is refused until the oldest ages out."""

    def __init__(self) -> None:
        self._fails: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._fails and self._fails[0] < now - LOCK_S:
            self._fails.popleft()

    def locked(self, now: float | None = None) -> float:
        """Seconds until sign-in is allowed again, 0 when it is."""
        t = time.time() if now is None else now
        self._trim(t)
        return self._fails[0] + LOCK_S - t if len(self._fails) >= MAX_FAILS else 0.0

    def failed(self, now: float | None = None) -> None:
        self._fails.append(time.time() if now is None else now)

    def reset(self) -> None:
        self._fails.clear()


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a path on this warden only, never another site."""
    t = target or "/inbox"
    return (
        t
        if t.startswith("/") and not t.startswith("//") and "\\" not in t
        else "/inbox"
    )


def page(
    css: str, csrf: str, target: str, error: str | None = None, enabled: bool = True
) -> str:
    e = html.escape
    head = (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>Sign in · print warden</title><style>{css}</style></head><body><main>'
        "<h1>Print warden</h1>"
    )
    if not enabled:
        body = (
            '<div class="card"><p>Sign-in is off. Set <code>PRINT_WARDEN_OPERATOR_PASSWORD</code> in the warden\'s '
            "environment (its <code>.env</code>) and restart it. The inbox, and its buttons, need it.</p></div>"
        )
    else:
        body = (
            '<div class="card"><form class="act" method="post" action="/signin">'
            f'<input type="hidden" name="csrf" value="{e(csrf)}"><input type="hidden" name="next" value="{e(target)}">'
            '<p><label>Operator password<br><input type="password" name="password" autocomplete="current-password" '
            'autofocus required style="font:inherit;padding:6px;width:100%;max-width:320px"></label></p>'
            + (f'<p class="bad">{e(error)}</p>' if error else "")
            + "<button>Sign in</button></form></div>"
        )
    return head + body + "</main></body></html>"
