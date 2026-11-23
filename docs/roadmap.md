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
- Default port 8710 again (0.7): the 0.3 move to 8720 assumed homelab-helper's status endpoint shared the host; it does not.

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

## Phase 22 — Real energy and cost per print ✅ (build)

- Optional `power_entity` per printer (a Home Assistant energy or power sensor): the kWh of each print, times a
  configured or live price, is a cost on the job; `printer_stats` splits filament from energy and shows idle heat-up.
  The cheap-power hold uses the measured draw.

Done when: a print's cost includes measured kWh within a few percent of the sensor's own total.

An energy counter is read at start, first layer and end; a power sensor is sampled every 30 s and integrated, with the
running total in the job store so a restart mid-print keeps it. The hold uses a printer's mean draw over the last
90 days of metered prints. A measured price per print uses the mean of the price at start and end, not an hourly
integral: close enough for a price that changes hourly and a print of a few hours, and said so here.

## Phase 23 — Multi-material (MMU and toolchangers)

- Klipper MMUs (Happy Hare / ERCF) and toolchangers: a filament per gate or tool, each its own Spoolman spool;
  per-tool material checks; Orca's multi-filament slicing with flush volumes from its profiles; spool usage per tool.
- Only if a multi-material printer is (or will be) in use: the MMU behaviour needs real hardware to validate.

Done when: a two-colour print is sliced, checked, started by tap and deducts both spools.

## Phase 24 — Why a print failed, and Reprint ✅ (build)

- A print that stops with a Klipper error records why: `print_stats.message`, the shutdown message, and the `!! `
  errors and last command from Moonraker's gcode store, classified (not homed, move out of range, cold extrusion,
  extrusion limits, unknown command, probe, file, and every shutdown cause) with what to check.
- The failure prompt leads with the cause and offers **Reprint**: the job's own gcode queued again, started by tap
  like any job. A failure labelled by hand is followed by the same offer.
- A cause seen twice on one printer in 14 days warns on `submit_job`; `printer_stats` counts causes per printer.

Done when: an error print, replayed, shows its cause on the phone and Reprint queues it.

## Phase 25 — Away mode ✅ (build)

- From Home Assistant presence (state only, never recorded): away, no auto-start and long prints are not offered
  unless allowed; alerts and the pause/cancel prompts ignore quiet hours. Back home: a "while you were out" summary.

Done when: away, a queued job is neither offered nor auto-started, and coming home brings the summary.

Offers are not held while away by default: a tap can come from anywhere, so only `away_offer_max_hours` holds them.
Unattended starts are, and an unreadable presence entity counts as away for them (the safe side) but not for alerts.

## Phase 26 — Printer health and config drift ✅ (build)

- Read-only through Moonraker: `printer.cfg` (and includes) diffed against a snapshot, Klipper / Moonraker / MCU
  firmware versions and mismatches, probe and mesh drift from the saved `bed_mesh` profiles. Findings feed
  homelab-helper's printwarden source.

Done when: a changed `printer.cfg` or a version mismatch shows as a finding before the next print.

The diff is of Klipper's parsed settings rather than the files, so includes need no fetching and comments or
reordering are not changes. `/healthz` carries only the finding kinds (details can name files); homelab-helper's
printwarden source still needs to turn them into findings.

## Phase 27 — Spool inventory ✅ (build)

Spoolman stays the system of record; the warden runs the physical workflows around it, from chat, the phone and a
`/spools` page (same token as `/status`, forms like `/stage`).

- **Register:** a new spool in one step (vendor, material, colour, net weight, empty-spool weight, price, where it is
  stored). The filament is matched or created in Spoolman, and a short spool code is shown for a label. Optional:
  an NFC sticker on the spool, bound with one Home Assistant tag scan.
- **Weigh-ins:** record a gross weight (scale reading); remaining = gross − empty-spool weight, written to Spoolman.
  The difference from the warden's counted use is kept per spool; a drift over ~5 % flags a wrong density or diameter.
  Periodic validation: a spool not weighed for `upkeep.weigh_every_days`, or after a set amount used, gets a phone
  prompt that takes the number as a reply.
- **Swap:** a guided switch-out per printer: weigh the outgoing spool (optional), pick or scan the incoming one, and
  the warden sets it active, checks it against the printer's queue (material, enough grams), and says if it is due
  for drying. A spool can be retired (empty) or moved to storage.

Done when: a new spool goes from the box to registered, loaded and weighed again with no Spoolman UI, and a
mis-set density shows as drift.

Built without the NFC sticker: the spool code on a label does the same job with no tag setup, and a Home Assistant
tag scan can follow when the codes prove too slow. The Spoolman writes (vendor, filament, spool, PATCH) are tested
against a mocked Spoolman; row 42 is the first real one.

## Phase 28 — Filament logistics ✅ (build)

- Reorder findings land on a Home Assistant to-do list. Optional humidity sensor per dry box replaces the days-open
  drying guess. Run-out forecast per material from recent use.
- Optional `spool_scale_entity` per printer (a load cell under the spool holder, e.g. HX711 + ESPHome): read while
  the printer is idle, less the spool's empty weight from Spoolman, it corrects the spool's remaining weight through
  the same weigh-in path as Phase 27.

Done when: a low material appears on the HA list by itself, and a scale reading corrects Spoolman.

## Phase 29 — Live sign-off sprint ✅ (build)

- ✅ `print-warden validate <row>` sets up what a live-validation row needs where it can (a pause left alone, an
  error gcode, a test batch), checks the result against the warden's own state, and records it: 21 rows check
  themselves or what follows from the operator's step, the rest are signed by hand; `validate report` fills in the
  sign-off table.
- ✅ `fault_test`: the error gcode, as a job that stops itself at layer 2 with a move Klipper refuses, kept out of
  the failure stats.

Done when: every row of `docs/live-validation.md` is signed or has an issue.

## Phase 30 — Rehearsal mode ✅ (build)

- ✅ `print-warden demo`: the warden, unchanged, against two simulated printers (Moonraker's HTTP routes and
  websocket, a print played layer by layer, pause/cancel/error/shutdown as on Klipper, history, a gcode store,
  printer.cfg settings and meshes), a simulated Home Assistant (alerts, notifications with buttons, entity states, the
  tap websocket) with a phone page, simulated cameras (the part on the bed until it is taken off) and a simulated
  slicer (`orca: sim`). Grace periods and the paused-print deadline run at the demo's speed.
- ✅ `tests/test_demo_e2e.py`: 15 rows of the runbook through `validate` against the demo on every CI run.

Done when: the checkable rows pass in CI against the simulator.

It found one bug on its first run: after a print stops with an error, Klipper leaves `print_stats` at `error` until
the next print, and the warden did not count that as idle, so nothing more was offered or started there (Reprint
included). `error` with Klipper ready is idle now.

## Phase 31 — The 1.0 contract ✅ (build)

- ✅ `docs/stability.md`: every public surface (CLI verbs, MCP tools and their arguments, HTTP routes, `/healthz`
  api 1, config sections, HA entities and MQTT topics, notification actions, the backup format) with what is
  promised about it, the safety invariants, and a deprecation policy. `tests/test_stability_doc.py` holds its
  tables to the code.
- ✅ `config.SCHEMA` lists every key the warden reads; `print-warden check` and the server's start-up warn on any
  other, with the closest known key.
- ✅ `print-warden backup` / `restore`: consistent database copies, profiles, bed references and sign-offs in one
  archive; restore refuses while a warden answers and keeps what it replaces. A database from the first release
  is upgraded in place (tested).

Done when: the contract is written and tested; 1.0 is cut once Phase 29's sign-off is complete on garagex4
(wyolax1, at another location, is not applicable for 1.0).

## Phase 32 — Weekly digest ✅ (build)

- ✅ `digest: {day, at}`: one phone summary a week. It covers prints done and failed (with the commonest reasons),
  hours, grams, kWh and cost, per printer and in all, then what is due: maintenance, drying, weigh-ins, reorders and
  materials running out within three weeks.
- ✅ It is computed from the rows the warden keeps, with no model, and fault tests are excluded. It is held while
  away and sent on return. The same numbers appear on `/status` ("This week") and from the `weekly_digest` tool.

Done when: a configured digest arrives once a week with the week's numbers, and waits while nobody is home.

## Phase 34 — Guided sign-off ✅ (build)

- ✅ `print-warden validate session --printer P --by EM` walks the runbook in order and asks each step on the
  phone (`ask_operator` / `operator_answer`, buttons that only record a tap).
  - A setup runs on Go.
  - A checkable row is polled until it passes (Skip meanwhile), and a failing one gets Retry / Fail with a note /
    Skip.
  - A row judged by eye is a Pass / Fail / Skip tap.
- ✅ It resumes, passing over signed rows. `--rows` limits it and `--terminal` asks at the terminal.
- ✅ It writes `<storage>/signoff-<printer>-<date>.md` with the table and each failure's note and checks. The PR
  that carries it is made from chat, since the warden has no GitHub access.

Done when: one printer's whole runbook can be signed in one session with the phone alone.

## Phase 35 — Record and replay ✅ (build)

- ✅ `print-warden record --printer P` subscribes to the printer's Moonraker as the warden does (read-only) and
  writes one print as JSON lines:
  - the snapshot, every status update and Klipper signal with its time;
  - then the gcode store and the history entry;
  - file names replaced with `recorded-<n>.gcode`.
- ✅ The simulator replays a recording whenever a print starts (`sim.SimPrinter(replay=...)`,
  `print-warden demo --replay demo-a=FILE`).
- ✅ `tests/test_record.py` replays every file in `tests/data/recordings/` to its recorded end. Under the real warden,
  the fault recording's diagnosis comes from the recorded gcode store, and the shutdown recording's from its
  message.
- The two recordings there now come from the simulator, so they prove the path, not Klipper. A replayed shutdown
  leaves the job printing, because the recording stops at Klipper's signal. What the printer reports after a shutdown,
  and whether the warden then settles the job, is the first thing a real recording will show.

Done when: CI replays at least one real error print and one real shutdown from each printer.

## Phase 36 — The week's plan ✅ (build)

- ✅ `plan.py`: each printer is a lane that is free after its current print plus a bed-clearing gap. Its queued
  jobs take, in order, the first 15-minute slot whose finish falls in its `finish_window` and whose start is outside
  quiet hours (`auto_start_hours` for an `auto_start` printer). The plan is pure and deterministic.
- ✅ `plan_week` and the Plan card on `/status` show "what prints when" and what each job waits for.
- ✅ `policy.plan_offers: true` holds the head of a queue until its slot and offers it then (checked every minute).
  It is off by default, so the plan only informs until it is turned on. A job with no slot in a week is offered as
  before, and nothing starts without a tap or `auto_start`.
- Cheap power is still the hourly price check before an offer; planning around a price forecast waits for a
  forecast source.

Done when: a mixed queue across both printers gets a plan that respects every window and is offered on time
(runbook row 46).

## Phase 38 — Plan around power prices ✅ (build)

- ✅ `energy.plan_by_price` makes the planner read a price forecast from a Home Assistant sensor's attributes.
  `energy.price_forecast_entity` names the sensor, defaulting to `price_entity`. It understands Nord Pool's
  `raw_today`/`raw_tomorrow`, any list of `{start, end, value}`-like intervals, and hourly `today`/`tomorrow` lists.
- ✅ A job moves to the cheapest slot within `energy.plan_max_delay_hours` only when the forecast covers the whole
  print and the mean price is at least 10 % lower. Finish windows, quiet hours and auto-start hours still apply.
- ✅ `plan_week` says why ("cheaper power (~0.1 vs 0.3 a kWh)"). With `policy.plan_offers` the offer waits for that
  slot. The forecast is read at most every 15 minutes.
- Cheap power's offer-time hold (`policy.cheap_power`) is unchanged.

Done when: a long job queued in the dear hours is planned, and offered, in the cheap ones within its windows.

## Phase 39 — Adding a printer, guided ✅ (build)

- ✅ `print-warden add-printer NAME --moonraker URL` reads Moonraker read-only. It gets Klipper's config (for the
  machine preset, as `profiles klipper`), the webcams (as `cameras:`, unless `--camera` names them) and the power
  devices (one becomes `power_device`).
- ✅ It builds the entry (location, Moonraker, cameras, power device, the `slicer:` block that `profiles block` would
  print) and prints it. `--write [PATH]` inserts it under `printers:` with every other line kept. It refuses a name
  already there, or any unknown key the old config did not have.
- ✅ It runs `check`'s profile and Moonraker rows for the new printer, then names the sign-off subset:
  `validate session --printer NAME --rows 0,1,3,4,11,12,39`.
- Bed checks, lights, windows and maintenance stay hand-written: they need the operator's measurements.

Done when: a new Klipper printer goes from its Moonraker URL to a passing `check` and a short sign-off with one
command and a restart.

## Phase 40 — The web inbox and browser sign-in ✅ (build)

- ✅ Every button prompt is recorded as it is sent (even when the phone did not get it), keyed by its notification
  tag. It leaves when the warden withdraws it, when it is answered on either side, or after a week.
- ✅ `/inbox` shows the waiting prompts with their buttons, text replies included. A button runs the phone tap's
  handler (`via` "the web inbox"), and answering in the inbox withdraws the phone's notification. An expired Start,
  or a job awaiting approval with no prompt left, offers `REOFFER` (Offer again).
- ✅ `PRINT_WARDEN_OPERATOR_PASSWORD` signs a browser in for 30 days (HMAC cookie bound to the password, HttpOnly,
  SameSite=Strict; five wrong tries lock it for 10 minutes). `/status`, `/stage` and `/spools` take the cookie or
  the token; `/inbox` takes only the cookie. The token is what MCP clients hold, so it never answers a prompt.
- ✅ With the password set, a tap on a notification's body opens the inbox; `/status` links to it with the count.
- ✅ A prompt whose job has moved on (Start after the job left awaiting approval; first layer, Cancel or Resume
  after the print ended) is dropped from the inbox and withdrawn from the phone, which never happened before.
- Another push channel (ntfy) is left for later: its taps would reach the same handler.

Done when: a Start swiped away on the phone is started from the inbox, and the token alone cannot do it.

## Phase 41 — Undo a mistaken tap ✅ (build)

- ✅ Every answer, from the phone or the inbox, is kept for a day: action, button, prompt, `via`, time, and why a
  start, cancel or resume was refused.
- ✅ The inbox's **Recent** section lists them. **Undo** is offered only while it is safe: Skip while the job is still
  skipped (it returns to `staged` and the queue offers it again); an outcome label while it is unchanged (cleared,
  and the outcome prompt is sent again); Bed cleared while the bed is still marked clear and no print has started
  (occupied again, and any offer made since is withdrawn).
- ✅ Start, Cancel and Resume act on the printer, so they are listed without an Undo.
- Answers given in chat (`mark_bed_clear`, `label_print`, `skip_job`) are in the job log, not in Recent.

Done when: a Skip tapped by mistake on the phone is undone from the inbox and the job is offered again.

## Phase 42 — Say what the printer is doing ✅ (build)

- ✅ `phase.describe` turns a printer's live status into a stage: heating (which heater, how far), start sequence
  (heaters at target with the nozzle held below printing temperature: homing, probing, a mesh), printing (layer N of
  M, or progress for files without layer info), paused, cooling, idle, offline, or Klipper's own state.
- ✅ `/status` shows it on every printer card, and says why the Bed cleared button is missing (a print running, the
  bed already clear, a job waiting for its Start, or the bed state unknown). `print_status` returns it as
  `activity`.
- ✅ A refused Bed cleared names the stage: "still printing (heating: bed 62/80 °C); clear the bed after the print
  ends", where it used to say "is printing; the bed cannot be clear" during heat-up.

Done when: a print heating up reads as heating, not as a stuck "printing 0 %".

