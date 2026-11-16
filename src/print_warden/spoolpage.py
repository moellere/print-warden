"""`/spools`: the spool inventory from a browser, behind the same token as `/mcp`, with the forms of `/stage`.

What is loaded where and what is on the shelf (code, name, grams left, last weighed), then four forms: register a
new spool, weigh one, swap a printer's spool, retire an empty one. Each posts with the page's CSRF token and comes
back to the page with the result. It never touches a print.
"""

from __future__ import annotations

from typing import Any

from .stage import _action, _e, _options, _page


def _hidden(csrf: str) -> str:
    return f'<input type="hidden" name="csrf" value="{_e(csrf)}">'


def render(
    data: dict[str, Any],
    csrf: str,
    token: str | None,
    message: str | None = None,
    error: str | None = None,
) -> str:
    """`data`: {loaded: {printer: spool|None}, stock: [spool], materials: [str], printers: [str]}; a spool carries
    code, name, material, remaining_g, location and weighed_days."""
    note = (f'<p class="ok">{_e(message)}</p>' if message else "") + (
        f'<p class="bad">{_e(error)}</p>' if error else ""
    )

    def row(s: dict[str, Any], where: str) -> str:
        weighed = s.get("weighed_days")
        return (
            f"<tr><td>{_e(s['code'])} {_e(s.get('name') or s.get('material'))}"
            f' <span class="mute">{_e(where)}</span></td>'
            f'<td class="r">{_e(round(float(s.get("remaining_g") or 0)))} g'
            f' <span class="mute">{"weighed " + format(weighed, ".0f") + " d ago" if weighed is not None else "not weighed"}</span></td></tr>'
        )

    loaded = "".join(
        row(s, f"on {p}")
        if s
        else f'<tr><td>{_e(p)}</td><td class="r mute">nothing loaded</td></tr>'
        for p, s in sorted(data["loaded"].items())
    )
    stock = "".join(row(s, s.get("location") or "") for s in data["stock"])
    hidden = _hidden(csrf)
    body = f"""{note}
<section class="card"><h2>Loaded</h2><table>{loaded}</table></section>
<section class="card"><h2>On the shelf</h2><table>{stock or '<tr><td class="mute">none</td></tr>'}</table></section>
<div class="grid">
<section class="card"><h2>Register a spool</h2>
<form method="post" action="{_action("/spools/register", token)}">{hidden}
<div class="row"><label>Vendor<input name="vendor" required></label>
<label>Material<select name="material">{_options(data["materials"], "PLA")}</select></label></div>
<div class="row"><label>Colour (name or #RRGGBB)<input name="color" required></label>
<label>Net weight g<input type="number" name="net_weight_g" value="1000" min="1"></label></div>
<div class="row"><label>Empty spool g<input type="number" name="spool_weight_g" min="0"></label>
<label>Price<input type="number" name="price" step="0.01" min="0"></label></div>
<label>Stored at<input name="location"></label>
<button type="submit">Register</button></form></section>
<section class="card"><h2>Weigh a spool</h2>
<form method="post" action="{_action("/spools/weigh", token)}">{hidden}
<div class="row"><label>Spool code<input name="spool" placeholder="S12" required></label>
<label>Scale reads g (spool included)<input type="number" name="gross_g" min="0" required></label></div>
<label>Empty spool g (only if Spoolman lacks it)<input type="number" name="empty_g" min="0"></label>
<button type="submit">Weigh</button></form></section>
<section class="card"><h2>Swap a spool</h2>
<form method="post" action="{_action("/spools/swap", token)}">{hidden}
<div class="row"><label>Printer<select name="printer">{_options(data["printers"], None)}</select></label>
<label>New spool code<input name="spool" placeholder="S12" required></label></div>
<label>Outgoing spool on the scale, g (optional)<input type="number" name="outgoing_gross_g" min="0"></label>
<button type="submit">Swap</button></form></section>
<section class="card"><h2>Retire an empty spool</h2>
<form method="post" action="{_action("/spools/retire", token)}">{hidden}
<label>Spool code<input name="spool" placeholder="S12" required></label>
<button class="alt" type="submit">Retire</button></form></section>
</div>"""
    return _page("Spools", body)
