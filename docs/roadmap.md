# Roadmap

Each phase lists what it delivers and when it is done. Phases 0–7 are complete, including the homelab-helper source; their open items shipped in 0.3.0. Phases 8–12 shipped
in 0.4.0; their live sign-off is `docs/live-validation.md`.

## Phase 0 — Hardening ✅

- Tests (policy, rules, job ledger, slicer header/presets, reconcile, retention, server routes) and CI: ruff, format, mypy, pytest.
- `/files` serves only `snapshots/`, `timelapse/`, `jobs/`; databases and uploaded models are never served.
- `PRINT_WARDEN_TOKEN` guards `/mcp` and `/upload`; `/ha/action` has its own `HA_ACTION_SECRET`; both webhooks fail closed.
- Daily retention: events, snapshots and timelapse frames older than `keep_days`.
- Job ledger enforces a transition table; log-only entries use `note()`.
- `.3mf` uploads were refused (they were sliced as STL; see Phase 4 for how 3MF is read now); model fetches by URL are
  capped at 100 MB.

Done when: CI green; `/files/events.db` 404; `/mcp` without the token 401; webhooks 403 without their secret.

## Phase 1 — Finish what was half-built ✅

- Spoolman: the job records its spool at approval; on completion or failure Klipper's `filament_used` is deducted
  (`use_length`); `can_start` blocks when the spool has less than the job's grams + 10%.
- Approval TTL: a Start tap answers one request and only within `policy.approval_ttl_minutes`; expired requests return
  to `staged` and the phone notification is withdrawn.
- Starts are serialised per printer, so concurrent taps or chat consents send one print.
- Restart recovery: on every Moonraker (re)connect, printing jobs are settled from Moonraker's history and an `approved`
  job orphaned by a crash returns to `staged`.
- Timelapse frames are rendered to mp4; the completion alert carries actual vs. estimated time, grams and the link.

## Phase 2 — Bed-clear check and `auto_start` ✅

- The bed is a recorded fact: a print starting or ending marks it `occupied`; a human clears it ("Bed cleared" phone
  button or quoted `mark_bed_clear`). Every start is blocked while it is occupied.
- Deterministic check: empty-bed reference per printer, cropped region, per-channel brightness matching, changed-pixel
  fraction vs. `threshold`. No model.
- Every moment a human knows the answer (after a print; on "Bed cleared") produces a labelled check; a new reference
  restarts the record.
- `auto_start` (per printer, opt-in): bed marked clear + live check clear + last `min_agreements` labelled checks agree
  + inside `auto_start_hours`. Unattended starts alert with the check's snapshot.
- Bed pose (bed-slingers with a frame camera): warden-sliced jobs park at `bed_check.pose` with steppers held;
  checks and references only at that pose; opt-in `rehome_y` (Y only, Z never moves).

## Phase 3 — Queue and scheduling ✅

- Per-printer queue (`queue`, `move_job`, `offer_next`); the next unblocked job is offered (tap or auto_start) after
  `submit_job` and whenever the bed is marked clear, one offer at a time.
- Location routing picks the printer with the material loaded that frees up soonest; `finish_window` keeps unattended
  starts from finishing at night and flags it on tapped requests.
- Slice checks: model fit (and `copies`) before slicing; extrusion extents, temperatures vs. material and
  `blocking_warnings` after. Findings block until `waive_blockers` with the operator's consent.
- `copies=N` (`--clone-objects` + `--arrange`) and `orient` (`--orient`).
- Presence-based quiet hours (0.3.0): `alerts.quiet_entity` / `quiet_states` mute like the time window.

## Phase 4 — Slicing quality ✅

- Slice cache keyed by geometry + final machine/process/filament settings + copies/orient/park + Orca binary.
- Per-model memory: geometry hash and overrides on every job; `model_history` and `submit_job`'s `history` show
  earlier settings and outcomes (advisory only).
- Per-spool temperatures from Spoolman (`settings_extruder_temp`, `settings_bed_temp`) as filament overrides.
- ASA, TPU, PLA-CF alongside PLA/PETG/ABS.
- Found by running OrcaSlicer 2.4.2 for real: the temperature check now judges the highest temperature set (the
  Artillery start gcode preheats to 140°C), extrusion counts only from a known position, and the house rules pick the
  High Temp Plate (Orca's default Cool Plate gave PLA a 35°C bed).
- 3MF projects (0.3.0): geometry only, one plate per job (`plate=N`); the project's settings and gcode are
  ignored, modifier volumes dropped, negative parts refused.

## Phase 5 — Monitoring depth ✅

- Rules over discovered objects: fan stall (tachometer fans), filament runout, board/host and configured temperature
  limits; Klipper shutdowns classified with a hint.
- First-layer check-in at a configurable layer (Looks good / Cancel print) and Resume / Cancel on Obico pauses, both
  bound to the job and refused if another file is printing. Cancel and resume are tap-only; no MCP tool.
- Alert routing: quiet hours for info/warning (critical always), configurable cooldown.

## Phase 6 — History and insight ✅

- Jobs record actual print time, grams and cost (Spoolman price); prints settled after a restart take them from
  Moonraker's history.
- `printer_stats`: estimate accuracy per printer/quality, failure rates and reasons by printer/material/quality,
  monthly prints/hours/grams/cost. `print_history` lists the jobs.
- ETA correction per printer (median of recent actual/estimate, ≥3 prints, clamped) used in routing, finish_window
  and notifications.
- HA entities per printer via the REST states API (state, progress, finish, queue, bed, material).
- MQTT discovery (0.3.0): with `home_assistant.mqtt` the same entities are published with unique_ids, one
  device per printer, retained, with availability; without it the REST approach needs no broker.

## Phase 7 — Packaging and ecosystem ✅ (warden side)

- Versioned releases: `v*` tags build and push `ghcr.io/moellere/print-warden` (tag checked against
  `pyproject.toml`); `CHANGELOG.md`, `docs/releasing.md`, `docs/operations.md`.
- `/healthz` status contract (api 1) with the version, for monitors.
- Previews drawn from the gcode, because headless OrcaSlicer 2.4.2 cannot render thumbnails (its CLI requests an
  OpenGL 3.4 context, which does not exist).
- homelab-helper reads `/healthz` as its read-only `printwarden` source (`helper discover printwarden`): warden
  unreachable, printer offline, Klipper not ready and stale telemetry become findings there.

## Phase 8 — Live validation and operations ✅ (0.4.0)

- `print-warden check`: profiles resolve; Moonraker (and its power device), cameras (must return an image), Home
  Assistant (token, notify service, lights, quiet entity), Spoolman and the broker answer. Read-only; exit 1 on failure.
- `docs/live-validation.md`: a runbook and sign-off table per printer.
- Default port 8720, off homelab-helper's 8710.

Done when: every sign-off row is signed on both printers, or has an issue.

## Phase 9 — Learn from how prints ended ✅ (0.4.0)

- After each print, the end photo on the phone with Good / Failed and a reason (adhesion, spaghetti, layer shift,
  under-extrusion, other); the stats report labelled failure reasons, not only Klipper's.
- A model that failed with given settings is flagged on `submit_job` (advisory, never a block).

Done when: failure rates by material and quality come from labelled outcomes.

## Phase 10 — Filament and maintenance ✅ (0.4.0)

- Reorder alert: remaining grams per material across Spoolman against what the queue needs.
- Drying reminders for hygroscopic materials by how long a spool has been open.
- Maintenance counters (print hours since nozzle change, lubrication, belt check) as entities, reset by a tap.

Done when: a low spool or an overdue service raises one alert, and a tap clears it.

## Phase 11 — Smarter scheduling ✅ (0.4.0)

- `copies=N` split across printers with the same material loaded.
- Optional cheap-power window from an HA price sensor for long prints, inside `finish_window`.

Done when: a 10-copy job on two idle printers finishes in about half the time, every start still tap- or
auto_start-gated.

## Phase 12 — Deeper homelab-helper integration ✅ (0.4.0)

- Warden events (Klipper shutdowns, repeated failures, overdue maintenance) in the status contract as an added field
  (api 1 only gains fields); homelab-helper turns them into findings.

Done when: a thermal-runaway shutdown appears in `helper findings list` with its classified cause.
