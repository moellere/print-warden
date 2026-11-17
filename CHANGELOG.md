# Changelog

## Unreleased

- `print-warden record --printer P` keeps one print as the printer reported it: status updates, Klipper signals,
  the gcode store and the history entry, with file names replaced. `print-warden demo --replay demo-a=FILE` plays
  a recording back, and CI replays every recording in `tests/data/recordings/`.
- `print-warden validate session --printer P --by EM` runs the whole runbook from the phone:
  - each row's setup runs on a Go tap, and each checkable row is checked until it passes;
  - a failing row offers Retry, Fail with a typed note, or Skip;
  - a row judged by eye is a Pass / Fail / Skip tap.
  It resumes where it stopped and writes `<storage>/signoff-<printer>-<date>.md`. Two new MCP tools,
  `ask_operator` and `operator_answer`, carry the questions; a tap on them only records the answer.

## 0.10.0

Rehearsal mode, the 1.0 contract and a weekly digest since 0.9.0. **Upgrade notes:** no config changes are needed.
- `print-warden check` and the start-up log now name config keys the warden ignores. A typo that silently did
  nothing before now shows up as a warning.
- A printer whose last print stopped with a Klipper error is idle again once Klipper is ready, so queued jobs are
  offered there as they would be after any other print.
- `digest:` is optional; without it, no weekly digest is sent.
- `docs/stability.md` says what is promised from 1.0.

- `print-warden demo` runs the warden against two simulated printers, a simulated Home Assistant with a phone page
  (`/sim/phone` on `--sim-port`), simulated cameras and a simulated slicer (`slicer.orca: sim`, no OrcaSlicer
  needed). Prints run `--speed` times faster than real time. `validate` works against it unchanged, and CI runs 15
  runbook rows end to end against it.
- Fix: after a print stops with a Klipper error, the printer is idle again once Klipper is ready. Before, nothing
  was offered or started on it until another print had run there; this included the Reprint offer.
- A Klipper shutdown from `M112` is classified as `emergency_stop`.
- `docs/stability.md` names every public surface, what is promised about each, and the deprecation policy that
  holds from 1.0. A test keeps its tables in step with the code.
- `print-warden check` warns on config keys the warden does not read ("did you mean …"); the server logs them at
  start.
- Weekly digest (`digest: {day, at}`): one phone summary a week. It covers prints done and failed with the
  commonest reasons, hours, grams, kWh and cost, plus what is due (maintenance, drying, weigh-ins, reorders,
  materials running out). It is held while away. The same numbers appear on `/status` and from the new
  `weekly_digest` tool.
- `print-warden backup` / `print-warden restore` keep the warden's state in one archive: the databases, profiles,
  bed references and validation sign-offs.

## 0.9.0

The live sign-off sprint's tooling since 0.8.0. **Upgrade notes:** no config changes are needed. `print-warden validate`
needs the warden running and reaches it on this config's port with `PRINT_WARDEN_TOKEN`; it keeps its results in
`<storage>/validation.json`. The new `fault_test` MCP tool stages a deliberately failing print, but only queues it:
a tap still starts it.

- `print-warden validate` runs the live-validation runbook against the running warden, over `/mcp` as any client
  does, so it starts nothing.
  - `validate` lists the rows with their sign-off per printer.
  - `validate <row> --printer P --setup --by <initials>` sets up what a row needs after a y/N: a test cube, its
    Start offer, a split, a batch, a `PAUSE` to a running warden print, the fault test, or the printer-health
    snapshot. It then checks the outcome against the warden's state and signs the row when the checks pass.
  - `validate sign` records a row judged by eye, or an issue link for one that failed.
  - `validate report` prints the sign-off table.
  - Results live in `<storage>/validation.json`.
- `fault_test(printer, requested_by)` queues a 20 mm cube that stops itself at layer 2: a move Klipper refuses
  ("Move out of range") before anything moves. It exercises error diagnosis, the Reprint prompt and the cool-down
  live, starts only on a tap, and stays out of failure stats, "failed before", repeat-cause warnings and `/healthz`'s
  `outcomes_7d`.
- The `mcp` dependency floor is now 1.30 (`validate` uses its `streamable_http_client`).

## 0.8.0

Failure diagnosis, away mode, printer health, spool inventory and filament logistics since 0.7.0. **Upgrade notes:**
no config changes are required; the new job columns are added on start-up and every new setting is optional
(`presence`, `upkeep.weigh_every_days`, `upkeep.todo_entity`, `upkeep.dry_boxes`, a printer's `spool_scale_entity`).
`/healthz` gains a `health` field, and printer health takes its known-good snapshot at the first hourly check. The
prompt after a print stops with a known error now offers **Reprint**.

- Filament logistics: `upkeep.todo_entity` puts each material to reorder on a Home Assistant to-do list and takes it
  off when stock recovers; `upkeep.dry_boxes` alerts on humidity over a box's limit; a run-out forecast per material
  (30 days of use) in `upkeep_status` and on `/status`; a printer's `spool_scale_entity` weighs its spool while idle.
- Spool inventory, with Spoolman as the record: `register_spool` (vendor and filament matched or created; the spool's
  code `S<id>` for a label), `weigh_spool` (remaining from a scale reading and the empty-spool weight; counted against
  weighed use, with a flag over 5 % drift), `swap_spool` (outgoing weighed, new one active, checked against the queue
  and drying), `retire_spool`. The `/spools` page does all four. `upkeep.weigh_every_days` sends a weigh-in prompt
  that takes the grams as a typed reply.
- Printer health, read-only: `printer_health(printer, accept)` and an hourly check compare each printer with a
  known-good snapshot: changed Klipper settings (includes resolved), a pending `SAVE_CONFIG`, MCU firmware from a
  different Klipper release, waiting updates, and saved meshes whose range grew. One warning alert per new set of
  findings; `/healthz` adds `health` (the finding kinds).
- Away mode (`presence.entity`, optional): while nobody is home, no unattended start (also while presence is
  unknown), quiet hours stop muting alerts, and `away_offer_max_hours` holds longer jobs. Coming home sends a "while
  you were out" summary and offers what waited. Presence is kept in memory only. `/status` shows "away".
- A print that stops with a Klipper error is diagnosed: the cause (not homed, move out of range, cold extrusion,
  extrusion limits, unknown command, probe, file, or a shutdown cause) and what Klipper said, with the last command,
  are kept on the job (`failure_cause`, `failure_detail`). The phone prompt leads with them and offers **Reprint**;
  a failure labelled by hand is followed by the same offer. `reprint(job_id, requested_by)` queues the job's own gcode
  again (starts on its own tap). A cause seen twice on one printer in 14 days warns on `submit_job`;
  `printer_stats` reports `klipper_errors` per printer. New job columns are added on start-up.

## 0.7.0

Safety and notification fixes since 0.6.0. **Upgrade notes:** a print left paused 30 minutes with no Resume is now
cancelled with the heaters off (`policy.paused_cancel_minutes: 0` keeps the old behaviour), and the default port is
8710 again (a config that sets `server.port` is unaffected; one relying on the 0.3–0.6 default of 8720 moves).

- **A failed print no longer leaves the printer hot.** Every cancel the warden sends is followed by
  `TURN_OFF_HEATERS` (Klipper's own `CANCEL_PRINT` keeps them on unless the printer's macro switches them off). A print
  paused for `policy.paused_cancel_minutes` (default 30; `0` turns it off) with no Resume is cancelled, heaters off;
  the pause alert and the Obico prompt say when. Two minutes after a print is cancelled or stops with an error, an idle
  printer still heating gets `TURN_OFF_HEATERS`. Each comes with a warning alert. **Upgrade note:** paused prints now
  cancel themselves after 30 minutes unless the config sets `policy.paused_cancel_minutes: 0`.
- A Klipper shutdown while the printer's `power_device` reads off is no longer a critical alert: the X1's board
  runs on USB backfeed with mains off and its link stalls hours later, which the start path already recovers
  (power on, FIRMWARE_RESTART). The event is logged at info, `/healthz` marks that `last_shutdown` `expected`,
  and the status page stops showing the printer as faulted. A failed power read still alerts.
- **Default port is 8710 again.** The 0.3 move to 8720 avoided homelab-helper's status endpoint, which
  runs on its own host, not the warden's; every deployment kept `server.port: 8710` and a hand-edited
  compose file to match. `compose.yml`, the image's `EXPOSE`, the docs and the example config follow.
  A config that sets `server.port` is unaffected.
- The Start / Skip approval notification is sent sticky and persistent (Android companion), so tapping its body or
  swiping no longer loses the request; the warden still withdraws it on start, skip or expiry.
- Every other button prompt (first layers, Obico pause, bed cleared, outcome, calibration, upkeep) is sticky too: a
  tap on its body no longer dismisses it, and opens the attached picture (`clickAction`) instead of the companion
  app's start page. They can still be swiped away.

## 0.6.0

Phases 19–22. No config changes are required: new job columns are added on start-up, and every new setting
(`purge_zone`, `power_entity`, `energy`, `policy.cheap_power.min_kwh`) is optional. Two things may show on upgrade:

- `print-warden check` notes that there is no profile snapshot yet; `print-warden profiles diff` takes one.
- A slice whose start gcode purges outside the printable area is now reported as a start-gcode purge (with a suggested
  `purge_zone`), no longer as the part extruding off the bed.

- `purge_zone` per printer: the start gcode's purge may extrude there, outside the printable area; the part is still
  held to the printable area. The extents check now tells the start gcode's purge apart from the part (Orca's `;TYPE:`
  marks) and, without a zone, suggests one.
- `print-warden profiles diff [--accept]`: snapshot the presets printers.yaml uses and list what changed after an
  OrcaSlicer upgrade; `print-warden check` warns while they differ.
- Batches: `submit_batch(where, items)` queues several prints (a 3MF's plates with `plates: "all"`) across printers,
  longest first to whichever would finish soonest, each an ordinary tap-started job; the result checks Spoolman stock
  for the batch. `batch_status`, saved batches (`save_batch`, `queue_saved_batch`, `list_saved_batches`,
  `delete_saved_batch`), and open batches on `/status`. New job column `batch_id`.
- `/stage`: add a print from a browser (file or link, where, material, quality, copies, plate): slice, see the
  preview, then Add to queue (offered on the phone as usual) or Discard. It never starts, cancels or waives; forms
  carry a CSRF token and must come from the page's own origin.
- Energy per print: a printer's `power_entity` (an HA kWh counter or W sensor) gives each job `energy_kwh`,
  `heatup_kwh` (before the first layer) and `energy_cost` (`energy.price_per_kwh` or a price entity);
  `printer_stats` reports kWh and energy cost per month and mean draw and heat-up per printer; `check` validates the
  entity's unit. `policy.cheap_power.min_kwh` holds by expected energy (measured draw) instead of hours.
- `print-warden check` validates `THINGIVERSE_TOKEN` when set; `--sites` also checks that Printables still answers the
  warden's query.

## 0.5.0

Phases 13–18. No config changes are required: new job columns are added on start-up, and every new setting is
optional. Some slices are re-run once rather than served from `slice-cache/`, because the settings passed to Orca
now carry the printer's compatibility.

- 3MF projects are sliced whole (modifiers, negative parts, per-object settings) with the printer's settings over
  them. Every gcode setting of the project is replaced and embedded gcode dropped first; a replaced setting that still
  reaches the output where the printer profile defines it blocks the job. `copies` / `orient` keep geometry-only.
- Part-on-bed check: `calibrate_bed_camera` (four bed corners in the bed camera) lets the post-print check compare
  where the first layer was drawn with what changed; a missing part or a cluttered bed re-sends the outcome prompt
  with a hint (also `outcome_hint` on the job, and `footprint_check(job_id)`). Advisory only.
- Calibration towers: `calibrate(printer, temp|flow|pa)` for the loaded spool; the best section, chosen on the phone
  or with `record_calibration`, is saved to the spool in Spoolman and applied to its later jobs (flow ratio and
  pressure advance as filament overrides, temperature in place of Spoolman's). Towers restore flow and pressure
  advance at the end. The extruder's `pressure_advance` is now read from Moonraker.
- `/status`: a read-only page (same token as `/mcp`) with every printer's state, progress, bed, queue, latest
  snapshot and maintenance due, plus filament to reorder or dry; refreshes every 30 s.
- Model pages: `submit_job` (and `submit_split`) take a Thingiverse (`THINGIVERSE_TOKEN`), Printables or any web
  page linking model files, with `file` to pick one; `model_page(url)` lists them. Source page, author and license
  are kept on the job and shown in `print_history`.
- Printer profiles for any OrcaSlicer printer: presets resolve by name across every bundled vendor (and Orca's
  filament library), not only `slicer.vendor_dir`, with parents found in their own vendor first. New
  `print-warden profiles` command: `vendors`, `printers <vendor>`, `block <printer>` (the printers.yaml slicer block),
  `import <files>` (Orca / Bambu Studio presets and bundles; `post_process` dropped) and `klipper <name> <moonraker>`
  (a printer profile from Klipper's config). New optional `slicer.user_dir` (default `<storage>/profiles`) and
  `slicer.profiles_root`.
- `print-warden check` honours `home_assistant.verify_tls: false`, as the warden already did; it used to fail an
  HA reached by IP with a certificate for its name.

## 0.4.0

Phases 8–12. **Upgrade note:** the default port is now 8720; a config that sets `server.port` is unaffected.
Everything else new is optional.

- **Default port is now 8720** (was 8710, which homelab-helper's status endpoint also uses). A config with
  `server.port: 8710` (as the example had) is unaffected; one relying on the default must set it or move its HA
  webhook, Obico webhook and MCP URLs to 8720. `compose.yml` and the image's `EXPOSE` follow.
- `print-warden check [config]`: resolves the Orca profiles and asks Moonraker, cameras, Home Assistant, Spoolman and
  the broker whether they answer, read-only; exit 1 on any failure.
- `docs/live-validation.md`: the runbook and sign-off table for the real printers.
- Print outcomes: the end photo with Good / Failed (and a reason) after every print, or `label_print` from chat;
  labels win in `printer_stats`, appear in `print_history` / `model_history`, and `submit_job` warns when the same
  model failed before with the same settings. New job columns are added on start-up.
- Upkeep: hourly reorder (Spoolman stock minus the queue under `upkeep.reorder_below_g`), drying
  (`upkeep.dry_after_days`, **Dried** button) and per-printer `maintenance` items (hours or days, **Done** button,
  `maint_<item>` entities); at most one phone alert per finding a day, never in quiet hours. MCP: `upkeep_status`,
  `record_maintenance_done`, `record_spool_dried`.
- `submit_split`: copies spread over printers with the material loaded, timed per printer, each share an ordinary
  job. `policy.cheap_power` holds offers of long prints while a price entity reads above `max_price`.
- `/healthz` (still api 1, fields added): `last_shutdown` (classified cause, until Klipper is ready again),
  `outcomes_7d` and `maintenance_due`, for homelab-helper's findings.

## 0.3.0

The three items the roadmap left open. Every new setting is optional; a 0.2.0 config runs unchanged.

- MQTT discovery: with `home_assistant.mqtt` the printer entities are published through the broker with
  unique_ids and a device per printer (retained; `print_warden/status` for availability). New dependency: aiomqtt.
- 3MF projects: `.3mf` uploads and URLs are accepted; `submit_job(plate=N)` slices one plate as geometry only (the
  project's settings and gcode are ignored), dropping modifier volumes and refusing negative parts.
- Presence-based quiet hours: `alerts.quiet_entity` and `alerts.quiet_states` mute info/warning alerts and the
  first-layer prompt while a Home Assistant entity is in one of those states; an unreadable entity never mutes.

## 0.2.0

Everything since the first cut, phase by phase (see `docs/roadmap.md`):

- **Hardening:** `/files` serves only snapshots, timelapses and job artifacts; `PRINT_WARDEN_TOKEN` guards `/mcp`
  and `/upload`; webhooks fail closed; daily retention; job state machine; tests and CI.
- **Spool, approvals, recovery:** Spoolman deduction and remaining-weight blocker; approval TTL; serialised starts;
  restart reconciliation from Moonraker history; timelapse mp4.
- **Bed:** bed state with a "Bed cleared" button; deterministic bed check against an empty-bed reference;
  `auto_start` gated on a clean labelled record; bed-check pose parked into the end gcode.
- **Queue:** per-printer queue, routing by soonest-free printer, `finish_window`, slice checks with waivable
  blockers, copies and orient.
- **Slicing:** slice cache, per-model history, per-spool temperatures, ASA/TPU/PLA-CF; High Temp Plate house rule
  (Orca's default gave PLA a 35°C bed); temperature check judges the printing temperature, not the preheat.
- **Monitoring:** fan stall, runout, board/host temperature, classified Klipper shutdowns; first-layer and Obico
  pause prompts with job-bound Cancel / Resume taps; quiet hours.
- **Insight:** per-job print time, grams and cost; `printer_stats`, `print_history`; per-printer ETA correction;
  Home Assistant sensors.
- **Packaging:** previews drawn from the gcode (headless Orca renders none); slice checks now read values written
  without a leading zero (`E.62349`), which Orca uses for most moves; `/healthz` status contract (api 1) with the
  version; tagged images on GHCR.
