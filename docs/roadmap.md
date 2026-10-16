# Roadmap

Each phase lists what it delivers and when it is done. Phases 0–2 are complete; the rest are proposals.

## Phase 0 — Hardening ✅

- Tests (policy, rules, job ledger, slicer header/presets, reconcile, retention, server routes) and CI: ruff, format, mypy, pytest.
- `/files` serves only `snapshots/`, `timelapse/`, `jobs/`; databases and uploaded models are never served.
- `PRINT_WARDEN_TOKEN` guards `/mcp` and `/upload`; `/ha/action` has its own `HA_ACTION_SECRET`; both webhooks fail closed.
- Daily retention: events, snapshots and timelapse frames older than `keep_days`.
- Job ledger enforces a transition table; log-only entries use `note()`.
- `.3mf` uploads are refused (they were sliced as STL); model fetches by URL are capped at 100 MB and must be STL/SCAD.

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

## Phase 3 — Queue and scheduling

- Per-printer queue; "bed cleared" offers (or auto-starts) the next job.
- Routing by soonest finish and quiet hours / HA presence, not only material + idle.
- Slice-time checks: bounding box vs. printable area/height, temperatures vs. material, Orca warnings as blockers.
- `copies=N` via Orca `--arrange` / `--orient`.

## Phase 4 — Slicing quality

- Slice cache keyed by model hash + presets + house rules + overrides.
- Per-model memory of overrides and outcomes.
- TPU / ASA; per-spool temperatures from Spoolman extra fields.
- Multi-plate 3mf projects.

## Phase 5 — Monitoring depth

- Rules: part fan stopped while commanded, runout sensor, MCU/chamber over-temp, classified Klipper shutdown reasons.
- First-layer check-in at layer 2–3 with "Looks good / Cancel" buttons.
- Actionable Obico pauses (Resume / Cancel taps — a tap is a human decision).
- Alert routing: quiet hours for info/warning, critical always through.

## Phase 6 — History and insight

- `print_history` / `printer_stats`: estimate accuracy (and ETA correction), failure rate by material/quality/printer,
  grams and cost per month.
- HA entities via MQTT discovery: state, progress, ETA, queue length, bed state.

## Phase 7 — Packaging and ecosystem

- Tagged images on GHCR from CI; versioned releases; operator docs.
- Optional read-only homelab-helper probe for warden and printer health.
