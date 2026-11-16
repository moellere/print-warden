"""Printer health: what changed on a printer since it was known good, read through Moonraker and never written.

- **Config:** Klipper's parsed settings (`configfile.settings`, every include already resolved) against a snapshot,
  one line per changed key; a `SAVE_CONFIG` still pending is its own finding. Saved mesh profiles are left to the
  mesh check.
- **Versions:** each MCU's firmware against the host Klipper (a different release, `v0.12` against `v0.11`, means
  rebuild and flash; Klipper refuses with a protocol error at the next restart), and updates the update manager
  sees for Klipper and Moonraker.
- **Mesh:** each saved `bed_mesh` profile's range (max − min) and mean against the snapshot; a range that grows by
  more than `MESH_RANGE_MM` is a bed or gantry that moved.

`snapshot` takes the current state as known good; `findings` compares. Both are pure; the server reads Moonraker.
"""

from __future__ import annotations

import json
import re
from typing import Any

MESH_RANGE_MM = 0.1
_RELEASE = re.compile(r"v?(\d+)\.(\d+)")


def _settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in settings.items() if not k.startswith("bed_mesh ")}


def mesh_stats(points: list[list[float]]) -> tuple[float, float] | None:
    flat = [float(z) for row in points for z in row]
    if not flat:
        return None
    return round(max(flat) - min(flat), 4), round(sum(flat) / len(flat), 4)


def meshes(bed_mesh: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Saved profile -> (range, mean) from Moonraker's `bed_mesh` object."""
    out = {}
    for name, prof in (bed_mesh.get("profiles") or {}).items():
        stats = mesh_stats(prof.get("points") or [])
        if stats:
            out[name] = stats
    return out


def snapshot(settings: dict[str, Any], bed_mesh: dict[str, Any]) -> dict[str, Any]:
    return {"settings": _settings(settings), "mesh": meshes(bed_mesh)}


def _short(v: Any) -> str:
    t = v if isinstance(v, str) else json.dumps(v)
    t = " ".join(str(t).split())
    return t if len(t) <= 60 else t[:57] + "..."


def config_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    out = []
    for sec in sorted(set(old) | set(new)):
        a, b = old.get(sec), new.get(sec)
        if a is None:
            out.append(f"[{sec}] added")
        elif b is None:
            out.append(f"[{sec}] removed")
        else:
            for k in sorted(set(a) | set(b)):
                if a.get(k) != b.get(k):
                    out.append(f"[{sec}] {k}: {_short(a.get(k))} -> {_short(b.get(k))}")
    return out


def release(version: str | None) -> tuple[int, int] | None:
    m = _RELEASE.search(version or "")
    return (int(m[1]), int(m[2])) if m else None


def version_findings(
    host: str | None, mcus: dict[str, str], updates: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    out = []
    want = release(host)
    for name, ver in sorted(mcus.items()):
        got = release(ver)
        if want and got and got != want:
            out.append(
                {
                    "kind": "firmware_mismatch",
                    "detail": f"MCU '{name}' runs {ver} but Klipper is {host}: rebuild and flash its firmware",
                }
            )
    for app in ("klipper", "moonraker"):
        u = updates.get(app) or {}
        cur, remote = u.get("version"), u.get("remote_version")
        if cur and remote and cur != remote and remote != "?":
            out.append(
                {
                    "kind": "update_available",
                    "detail": f"{app} {cur} -> {remote} available",
                }
            )
    return out


def findings(
    snap: dict[str, Any] | None,
    settings: dict[str, Any],
    bed_mesh: dict[str, Any],
    save_pending: bool,
    host: str | None,
    mcus: dict[str, str],
    updates: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Everything worth a look, each {kind, detail}. With no snapshot, only what needs none."""
    out: list[dict[str, str]] = []
    if save_pending:
        out.append(
            {
                "kind": "save_config_pending",
                "detail": "SAVE_CONFIG has changes not yet written to printer.cfg (they are lost at the next restart)",
            }
        )
    if snap is not None:
        for line in config_changes(snap.get("settings") or {}, _settings(settings)):
            out.append({"kind": "config_changed", "detail": line})
        now = meshes(bed_mesh)
        for name, (rng, mean) in sorted(now.items()):
            was = (snap.get("mesh") or {}).get(name)
            if was and rng - was[0] > MESH_RANGE_MM:
                out.append(
                    {
                        "kind": "mesh_drift",
                        "detail": f"mesh '{name}' range {was[0]:.3f} -> {rng:.3f} mm (mean {was[1]:+.3f} -> {mean:+.3f}): "
                        "the bed or gantry moved; re-level or re-mesh",
                    }
                )
    out += version_findings(host, mcus, updates)
    return out
