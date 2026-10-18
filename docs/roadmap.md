# Roadmap

Each phase lists what it delivers and when it is done. Phases 0–7 are complete, including the homelab-helper source; their open items shipped in 0.3.0. Phases 8–12 shipped
in 0.4.0; their live sign-off is `docs/live-validation.md`. Phases 13–18 are the next round.

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

## Phase 13 — Live sign-off

- `docs/live-validation.md` walked on both printers; each failed row becomes an issue and a 0.4.x fix.
- homelab-helper: a printer removed from the warden resolves its findings (the status lists every printer); a
  homelab-helper release with the printwarden source when the operator names the version.

Done when: every sign-off row is signed or has an issue.

## Phase 14 — Full 3MF projects ✅ (build)

- A 3MF is sliced as the project (modifiers, negative parts, per-object settings) with the printer's settings loaded
  over it. Every gcode-bearing setting of the project (`*_gcode`, `post_process`) is replaced by a sentinel and
  embedded gcode is dropped; a sentinel in the output for a setting the printer profile defines blocks the job.
- Found by slicing for real: OrcaSlicer 2.4.2 keeps the project's value for gcode settings the printer profile does
  not define (`file_start_gcode`, the extrusion-role change scripts), so without the sanitizer a project could run
  its own gcode at file start. Orca crashes on a project with no `project_settings.config`, so it is kept, cleaned.
- `copies` / `orient` re-arrange the plate and fall back to geometry only.

Done when: a plate with a negative part slices with its hole, and no project gcode reaches the printer.

## Phase 15 — Did the part stay on the bed ✅ (build)

- A one-time bed-camera calibration (four bed corners tapped in a reference image) maps bed coordinates to the image;
  after a "complete" print, at the parked pose, the footprint the gcode drew is compared with what changed. Nothing
  there pre-fills the outcome as "probably failed"; a person still confirms. Deterministic, no model.
- The footprint is Orca's part extrusion on the first layers (`;TYPE:` marks drop the purge line, skirt and prime
  tower), split into parts by connected region, so one detached part of three is named.

Done when: a removed part is flagged before anyone looks.

## Phase 16 — Per-spool tuning and calibration prints ✅ (build)

- Flow ratio and pressure advance per spool in Spoolman extra fields, applied as filament overrides.
- `calibrate(printer, spool, kind)` generates a temperature / flow / pressure-advance print; the outcome prompt asks
  which section was best and records it on the spool.
- Found by slicing for real: a tower whose sections alternated in width overhung every other section and brought
  tree supports (97 extra support layers); the tower now only steps inward. Klipper keeps `M221` and pressure
  advance after a print, so each tower ends by restoring them (the live pressure advance comes from Moonraker).

Done when: a new spool goes from calibration print to tuned settings on the next job without editing a profile.

## Phase 17 — Read-only status page and model intake ✅ (build)

- `/status` (token-guarded): printers, queues, ETAs, bed state, last snapshots, upkeep due. Actions stay on the phone.
  Snapshots load from the open `/files` route, so the token is not copied into image URLs.
- A Printables / Thingiverse page URL resolves to its model files, with license and author kept on the job; any
  other page yields the model files it links. Thingiverse is its v1 API (app token); Printables has no public API, so
  the warden uses the GraphQL endpoint its site calls. Neither site is reachable from the build container, so both
  are covered by mocked tests and wait on a live row.

Done when: the page shows both printers live and `submit_job` takes a model page URL.

## Phase 18 — Printer profiles for any printer ✅ (build)

- Use any OrcaSlicer vendor's profiles, not only Artillery: list vendors/printers in Orca's bundled profiles and
  write the `printers.yaml` slicer block for one.
- Import a user's own Orca/Bambu printer, process and filament presets (exported `.json` or `.orca_printer` /
  `.bbscfg` bundles) into a warden profile directory, resolving `inherits` against the bundled vendors.
- Generate a printer profile from Klipper itself (bed size from `printer.cfg` limits via Moonraker, nozzle, kinematics,
  start/end macros) on top of a generic base when no vendor profile fits.

Done when: a printer not in the Artillery set is configured from scratch with one command and slices correctly.

Found by slicing for real (OrcaSlicer 2.4.2):

- 465 bundled presets have a file name that is not their `name`, and `fdm_machine_common` and friends exist in many
  vendors with different contents, so presets are indexed by `name` and a parent resolves in its child's vendor
  first, then Orca's filament library. All 9,834 instantiated bundled presets resolve.
- Orca's CLI refuses (rc -17) a machine whose name or `from: User` it cannot match to a bundled printer. A user's
  preset is therefore passed with `inherits` naming its nearest bundled ancestor, and a process whose compatibility
  list does not name that printer gets it added (printers.yaml already pairs them).
- A bundled printer may inherit a sibling (another nozzle); counting the whole chain as the same printer gave a 0.2 mm
  nozzle the 0.4 mm processes, so only a user's copy matches its bundled parent.
- Cubicon's bundled filaments set `default_filament_colour` to a string, which aborts Orca's CLI; it is passed as a
  list.
- 34 printers sampled across 27 vendors, plus the Creality K1, a profile from a Klipper config and an imported Orca
  bundle, sliced a cube with the generated block. All but one had no blocker: the Anycubic Kobra 2 Pro's start gcode
  purges at Y226 on a 220 mm printable area, which the extents check blocks.


## Phase 19 — Profile and check polish ✅ (build)

- A per-printer `purge_zone`: the start gcode's purge may extrude there, outside the printable area, while the part
  stays strictly on it (bundled start gcode such as the Anycubic Kobra 2 Pro's purges at Y226 on a 220 mm area).
- `print-warden profiles diff`: a snapshot of every preset printers.yaml uses, flattened; after an Orca upgrade, the
  settings that changed per printer, so a profile change does not alter prints silently.
- `print-warden check` validates the Thingiverse token, and with `--sites` that Printables still takes the warden's
  query.

Done when: the Kobra 2 Pro slices without a waiver, and an Orca upgrade lists what changed per printer.

Found by slicing for real: after `G28` the Kobra's X is unknown to the gcode, so only the purge line's end is known;
the suggested zone is therefore the strip along the bed edge the purge crossed, not the purge's own (partial) box.
With `purge_zone: [0, 222, 220, 230]` the Kobra 2 Pro's bundled profile slices with no blocker.

## Phase 20 — Multi-plate projects and batches ✅ (build)

- A 3MF with several plates becomes one batch of jobs, routed across printers like `submit_split`; each job still
  starts on its own tap.
- Saved batches ("the usual set") re-queued with one call; a batch's combined estimate and filament checked against
  Spoolman stock.

Done when: a 4-plate project is queued in one call, prints across both printers, and its batch shows progress.

Each plate is timed on every printer as the job itself will be sliced (the project, its printer's settings), so the
real job usually comes from the slice cache. Assignment is the longest-processing-time rule: longest first, each to
the printer that would finish it soonest.

## Phase 21 — A staging page ✅ (build)

- `/status` gains upload, material and quality, a slice preview and "add to queue". Nothing on it starts, cancels or
  waives: those stay on the phone tap. Same token, plus a CSRF token on its forms.

Done when: someone on the household network uploads an STL and it appears as an offer on the phone.

Slicing stages the job without queueing it, so a look at the preview comes before the queue; Add to queue and Discard
only touch jobs the page staged. The CSRF token is an HMAC of the hour (a per-process secret when the warden runs
without a token, which is when it matters most).

## Phase 22 — Real energy and cost per print

- Optional `power_entity` per printer (a Home Assistant energy or power sensor): the kWh of each print, times a
  configured or live price, is a cost on the job; `printer_stats` splits filament from energy and shows idle heat-up.
  The cheap-power hold uses the measured draw.

Done when: a print's cost includes measured kWh within a few percent of the sensor's own total.

## Phase 23 — Multi-material (MMU and toolchangers)

- Klipper MMUs (Happy Hare / ERCF) and toolchangers: a filament per gate or tool, each its own Spoolman spool;
  per-tool material checks; Orca's multi-filament slicing with flush volumes from its profiles; spool usage per tool.
- Only if a multi-material printer is (or will be) in use: the MMU behaviour needs real hardware to validate.

Done when: a two-colour print is sliced, checked, started by tap and deducts both spools.
