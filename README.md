# print-warden

Print manager for the homelab's 3D printers so a Claude session can hand off everything after design.
Slices with a headless OrcaSlicer (Artillery vendor presets + house rules), stages gcode on the printer,
starts jobs only with the operator's permission, then watches: deterministic rules over Moonraker telemetry,
Obico's spaghetti verdicts via webhook, snapshots from any HTTP camera, timelapse frames per layer, and
alerts fanned out through one Home Assistant webhook. Exposes an MCP server (Streamable HTTP at `/mcp`).

## Trust gradient

| Action | Who decides |
|---|---|
| Slice, stage gcode, snapshots, alerts, work light | the warden, automatically |
| Mark the bed clear | the operator: the "Bed cleared" phone button, or quoted chat permission (`mark_bed_clear`) |
| Start a print | the operator: a phone tap (`request_start`) or quoted chat permission (`start_print`), and `policy.can_start` must pass |
| Start without a tap | the warden, only on an `auto_start` printer whose bed a human cleared, whose bed check agrees now and has a clean record, inside `auto_start_hours` |
| Pause on detected failure | Obico |
| Cancel or resume a running print | a human: at the printer, or the Cancel / Resume button on a warden notification for that job (no MCP tool can) |
| Power off | a human |

Printers with a `power_device` (the X1's Sonoff) are powered on at start and Klipper is coaxed to ready with
up to three FIRMWARE_RESTARTs before the print command goes out; the tap still gates the whole sequence.

`policy.can_start` is plain Python: Moonraker connected, Klipper ready, printer idle, gcode staged,
loaded material matches the job, and the active spool has the job's grams plus 10%. No model is consulted in the gate.
A phone tap answers one `request_start` and is honoured only within `policy.approval_ttl_minutes` (default 15);
unanswered requests go back to `staged` and the notification is withdrawn. The Start / Skip notification is sticky on
Android: a tap on its body or a swipe does not dismiss it, only its buttons or the warden's own withdrawal (start, skip,
expiry) do. Starts on one printer are serialised, so a
double tap sends one print.

## Monitoring

Rules run on every Moonraker update; besides heaters, stalls and connection loss they watch whatever the printer has,
discovered from Moonraker's object list on each (re)subscribe:

- **Fans** (`fan`, `heater_fan …`, `controller_fan …`, `fan_generic …`): commanded ≥30% while the tachometer reads
  under `fan_min_rpm` for `fan_stall_s` → critical. Fans without a tachometer (`rpm` is null) are not judged.
- **Runout sensors** (`filament_switch_sensor …`, `filament_motion_sensor …`): filament lost mid-print → warning.
- **Temperature sensors**: any whose name looks like a board or host (mcu, host, rpi, cpu) over 75°C, or any sensor
  over its `rules.temp_limits` entry → warning, once until it cools 5°C.
- **Klipper shutdowns** are classified (thermal runaway, heater not heating, thermistor out of range, MCU comms lost,
  timer too close, TMC driver error, move out of range) with what to check.

At layer `first_layer_check` (default 2; 0 disables) the phone gets the nozzle camera frame with **Looks good** /
**Cancel print**. When Obico pauses a print the phone gets **Resume** / **Cancel print**. Both are bound to the job:
the tap only acts if that job is still the one printing (same file on the printer), so a stale button cannot touch a
later print. `alerts.quiet_hours` mutes info and warning alerts and the first-layer prompt, as does `alerts.quiet_entity` while
that Home Assistant entity is in `alerts.quiet_states` (default `["on"]`; an unreadable entity never mutes); critical alerts and
Obico pauses always go out. `alerts.cooldown_s` (default 300) spaces repeats of the same alert.

## History and Home Assistant

Every finished job records Klipper's actual print time, the grams used (Klipper's filament length scaled by the
slicer's grams per metre) and, when Spoolman has a price for the spool (spool price / initial weight, else the
filament's), the cost. `printer_stats(days=90)` reports estimate accuracy per printer and quality, failure rates by
printer / material / quality with the commonest reasons, and prints, hours, grams and cost per month;
`print_history(printer, days, state)` lists the jobs behind it.

**Energy.** With a printer's `power_entity` (a Home Assistant sensor on its plug), every print records its kWh, the
heat-up before the first layer, and an energy cost. An energy counter (kWh, Wh) gives the exact rise from start to
end; a power sensor (W, kW) is sampled every 30 s and integrated, and a gap over 5 minutes (the warden was down)
counts nothing. The price is `energy.price_per_kwh`, or the mean of `energy.price_entity` (default: the cheap-power
entity) at start and end. `printer_stats` adds kWh and energy cost per month and, per printer, mean draw while
printing and mean heat-up; `print-warden check` confirms each power entity reports a unit it can use.

Klipper knows a print completed, not whether it is any good. After every print the phone gets the end photo with
**Good** / **Failed** (a cancelled or errored print goes straight to the reasons): adhesion, spaghetti, layer shift,
under-extrusion, other. The label (or `label_print(job_id, result, labelled_by, reason)` from chat) wins over how the
job ended in the failure rates and reasons, and `submit_job` warns when the same geometry failed before with the same
material, quality and overrides (advisory; it never blocks).

**Calibration towers.** `calibrate(printer, kind)` slices a five-section tower for the spool loaded on the printer and
queues it like any job (it starts on a tap). `temp` steps the nozzle from 10 °C above the spool's temperature to 10
below, `flow` the extrusion multiplier from 104 % to 96 %, `pa` Klipper's pressure advance (`bowden=True` for a
Bowden range). The warden inserts each section's command after slicing, checks temperatures as usual, and puts flow and
pressure advance back at the end. Afterwards the phone asks which section looked best; the answer is saved on the
spool in Spoolman (`pw_nozzle_temp`, `pw_flow_ratio`, `pw_pressure_advance`; `record_calibration` from chat) and that
spool's later jobs are sliced with it.

Upkeep runs hourly and sends each finding to the phone at most once a day, outside quiet hours:

- **Reorder:** a material whose Spoolman stock, minus what the queue still needs, is under
  `upkeep.reorder_below_g`.
- **Drying:** a loaded spool of a material in `upkeep.dry_after_days` that has been open (or since it was last dried)
  longer than that. The **Dried** button records it.
- **Maintenance:** per printer, `maintenance: {nozzle: {every_hours: 300}, belts: {every_days: 90}}`. Hours are the
  print time since the item was last done. **Done** resets it, as does `record_maintenance_done` from chat. Each item
  is a `sensor.print_warden_<printer>_maint_<item>` showing the share of its interval used. A new item starts
  counting when it is first seen.

`upkeep_status` shows all three.

Each printer's ETA correction is the median actual/estimate ratio of its last 20 completed prints (once there are 3,
clamped to 0.5–2×). It is applied wherever an estimate is used: routing, `finish_window`, notifications and
`est_human` ("1h 12m (Orca 1h 0m)").

With `home_assistant.publish_entities` (default on) the warden keeps `sensor.print_warden_<printer>_{state, progress,
finish, queue, bed, material}` up to date through HA's REST states API, using the token it already has: changes every
30 s, everything every 5 min (HA forgets REST-set states on restart). These entities have no unique_id, so they cannot
be renamed or assigned to an area in the HA UI. With `home_assistant.mqtt` set (the broker HA's MQTT integration uses)
the same entities are published by MQTT discovery instead: same entity ids, but with unique_ids and one device per
printer, retained on the broker so they survive an HA restart, and unavailable while the warden is down.

## Bed-clear check and auto_start

The bed is a recorded fact: a print starting or ending marks it `occupied`, and every start is blocked until a human
marks it clear (the "Bed cleared" phone button sent after each print, or `mark_bed_clear` with the operator's words).

With `bed_check` configured, the first "Bed cleared" also captures the empty-bed reference from that camera (light on).
After that, the warden compares the camera to the reference whenever a human knows the answer: ~30 s after a print
ends (should read occupied) and on every "Bed cleared" (should read clear). The comparison is plain image arithmetic:
crop to `region`, match brightness per colour channel, count pixels that differ. `score` is the fraction that
differs; `threshold` (default 0.002) separates clear from occupied. Use `check_bed` to see live scores and tune it,
and `capture_bed_reference` after changing the build plate (the record starts over).

On a bed-slinger with a frame-mounted camera the bed's Y position changes the picture, so set `bed_check.pose`. The
warden then edits the end gcode of every job it slices: a park block (Z up to at least `pose.z` and clear of the part,
bed to `pose.y`) goes in before the stepper-off line, and the `M84`/`M18` lines are dropped so the steppers hold the
bed until Klipper's `idle_timeout` releases them (raise `idle_timeout` if you clear beds later than its default
10 min). Checks and reference captures run only while the toolhead is homed at the pose; otherwise the check reports
"not at the bed-check pose" and records nothing, and auto_start refuses. With `rehome_y: true` an idle printer whose
steppers were released is brought back with `G28 Y` and a Y move (X too if the pose sets it); Z never moves, the last
commanded Z must be at least `pose.z`, and every rehome alerts. Prints the warden did not slice have no park block.

`auto_start: true` starts the queue's next job without a tap only when the bed is marked clear, a live check reads clear,
the last `min_agreements` labelled checks all agreed with the operator, and the local time is inside
`auto_start_hours`. Each unattended start sends an alert with the check's snapshot. A missed small part (a false
"clear" after a print) breaks the record, so auto_start stops until the check earns it back.

**Did the part stay on the bed.** Klipper reports a print complete even when the part came off halfway. Once the bed
camera is calibrated (`calibrate_bed_camera`: the printable area's four corners in a snapshot at the bed-check pose,
as image fractions), the post-print check also projects where the job's first layer was drawn into the frame. If a
part is not seen there ("probably failed: 1 of 3 parts not seen..."), or much of the bed away from the parts changed
(debris, spaghetti), the outcome prompt is re-sent with that hint. The hint is kept on the job (`outcome_hint`); a
person still decides. `footprint_check(job_id)` runs it on demand. Deterministic image arithmetic, no model.

## Job flow

```
curl -F file=@part.stl http://nas.local:8720/upload           # or a .scad / .3mf
submit_job(printer="garagex4", model="part.stl", material="PETG", quality="standard", copies=1)
   -> checks the model fits, slices (tree supports, no brim), checks the gcode, uploads it to Moonraker as
      warden/<job>_<name>_<mat>.gcode and queues it; printer may be a location ("wyola")
job_status(job_id)          # estimates, warnings, slice blockers, can_start + blockers
queue("garagex4") / move_job(job_id, 0) / offer_next("garagex4")
request_start(job_id)       # phone notification with Start / Skip buttons (the queue does this for you)
waive_blockers(job_id, authorized_by="the operator said: print it anyway")
start_print(job_id, authorized_by="the operator said: go ahead and print it")   # chat path
list_spools() / set_active_spool("garagex4", 2)   # Spoolman owns spools; the warden records which one is loaded
```

When a print ends the warden deducts the filament Klipper reports from the job's spool in Spoolman (set
`track_spool_usage: false` on a printer whose Moonraker runs its own `[spoolman]` component), renders the per-layer
frames into `timelapse.mp4`, and sends the completion alert with actual vs. estimated time, grams and the timelapse link.
On every Moonraker (re)connect, jobs whose outcome happened while the warden was away are settled from Moonraker's
print history.

**Queue.** Staged jobs line up per printer (`queue`, `move_job`). The warden offers the next one (phone tap, or
`auto_start`) when the printer is idle, its bed is not occupied and nothing is already waiting: right after
`submit_job`, and whenever the bed is marked clear. One offer at a time; a Skip or an expired request offers nothing
more until the next trigger or `offer_next`. `submit_job(..., offer=False)` stages a job without queueing it.

**Routing.** A location picks the printer with the job's material loaded that frees up soonest (what is left of the
current print plus its queue); printers that cannot print are skipped. `finish_window` ("07:00-23:00") keeps
auto_start from beginning a job that would finish outside it, and puts the expected finish in a tapped request.

**Splitting.** `submit_split(where, model, material, copies)` spreads copies over the printers at a location (or
`any`, or `"a,b"`) that have the material loaded. Each printer is timed with one copy (corrected, cached). Copies then
go to whichever printer would finish its share soonest, within what fits on its plate. Every share is an ordinary job,
offered and started by tap or `auto_start` like any other. The result shows the expected finish against one printer
doing it all.

**Batches.** `submit_batch(where, items)` queues several prints at once; each item is `{model, material, quality?,
plates?, copies?, file?, overrides?, name?}`, and `plates: "all"` makes one job per plate of a 3MF project. Every job
is timed on each printer that can take it (material loaded, profile, fits its plate); the longest go first, each to the
printer that would finish it soonest. Every job is ordinary: queued, offered and started by tap or `auto_start`. The
result says when each printer should be done and whether Spoolman's stock covers the batch after the rest of the
queue. `batch_status(batch_id)` shows progress (also on `/status`); `save_batch` / `submit_batch(..., save_as=...)`
keep a set ("the usual set") for `queue_saved_batch(name)`; `list_saved_batches`, `delete_saved_batch`.

**Cheap power.** With `policy.cheap_power` (`entity`, `max_price`, `min_hours`), the offer of a job expected to run at
least `min_hours` waits while the price entity reads above `max_price`, and is retried hourly. With `min_kwh` and a
metered printer, the job's expected energy (its corrected estimate times the printer's measured draw over 90 days)
decides instead of its hours. An unreadable price never holds a job, and an explicit `request_start` is never held.

**Slice checks.** Before slicing, the model's bounding box (rotated 90° if that fits) and `copies` are checked against
the machine's printable area and height; a model that cannot fit is refused. After slicing, the extrusion extents are
checked against the bed, the nozzle and bed temperatures against the material's range, and Orca's log against
`slicer.blocking_warnings`. Findings become slice blockers: `can_start` refuses and the queue passes the job over
until `waive_blockers` records the operator's consent. `copies` uses Orca's `--clone-objects` + `--arrange`;
`orient=True` uses `--orient` (the pre-slice fit check is skipped then; the gcode check still runs).

**Slicing quality.** An identical request (same model geometry, same final machine/process/filament settings,
copies, orient, park pose and Orca binary) reuses the earlier slice from `slice-cache/` (pruned after `keep_days`
unused). Each job records a hash of its geometry and the overrides used: `model_history("bracket.stl")` lists earlier
prints of that model with their settings and outcomes, and `submit_job` returns the same summary as `history`, so a
print that worked can be repeated and one that failed is not retried blindly. Nothing is applied automatically. With
`use_spool_temps` (default) the loaded spool's Spoolman temperatures (`settings_extruder_temp`, `settings_bed_temp`)
replace the profile's when its material matches the job. The house rules set `curr_bed_type: High Temp Plate`:
OrcaSlicer otherwise picks the Cool Plate temperatures, which in the Artillery profiles is a 35°C bed for PLA.

Qualities: draft (0.24), standard (0.20), strength (0.20), fine (0.12). Materials: PLA, PETG, ABS, ASA, TPU,
PLA-CF (Artillery Generic profiles; temperature checks cover all six).
Models are STL, SCAD or 3MF, up to 100 MB. A 3MF project (Orca, Bambu Studio, PrusaSlicer) is sliced whole, so
modifiers, negative parts and per-object settings apply, with this printer's machine, process and filament settings
loaded over it; `plate` picks one plate per job. Before slicing, every gcode setting in the project (`*_gcode`,
`post_process`) is replaced and embedded gcode dropped, so a downloaded project cannot run its own gcode; if one of
those settings still reaches the output where the printer profile defines it, the job is blocked. With `copies` or
`orient` a 3MF is sliced as geometry only.
**Model pages.** `model` may also be a model page: `model_page(url)` shows its title, author, license and model
files, and `submit_job(..., model=<page>, file="lid")` prints one of them (`file` is needed when there are several).
The page, author and license stay on the job (`print_history`). Thingiverse goes through its API and needs an app
token in `THINGIVERSE_TOKEN` (`intake.thingiverse_token_env` to rename it); Printables goes through the GraphQL
endpoint its site uses, which is unofficial and may change; any other page is searched for links ending in
`.stl` / `.3mf` / `.scad`, with `og:title`, the `author` meta and a `rel="license"` link.
`overrides` on `submit_job` are OrcaSlicer process keys applied last (e.g. `{"enable_support": "0"}`).

## Printer profiles

Any OrcaSlicer printer works, not only the Artillery set: presets are looked up by name in the warden's own profile
directory (`slicer.user_dir`, default `<storage>/profiles`), then `slicer.vendor_dir`, then Orca's filament library,
then every other vendor next to it (`slicer.profiles_root`, default the vendor directory's parent). A preset's parents
resolve in its own vendor first, as in Orca. `print-warden profiles` sets a printer up:

```
print-warden profiles vendors                                  # Orca's bundled vendors
print-warden profiles printers Creality                        # that vendor's printers
print-warden profiles block "Creality K1 (0.4 nozzle)"         # the printers.yaml slicer block to paste
print-warden profiles import my-k1.orca_printer "My PETG.json" # your own Orca / Bambu Studio presets or bundles
print-warden profiles klipper "Voron 350" http://voron.local:7125   # a printer profile from Klipper's config
```

`block` keys the processes Orca lists as compatible with the printer by quality (`standard`, `draft`, `fine`, ...;
a repeated name gets its layer height, `standard_0.28`), and picks per material the warden checks your own filament,
else the vendor's generic one, else Orca's universal one. `import` writes presets (`.json`, `.orca_printer`,
`.orca_filament`, `.bbscfg`, `.bbsflmt`) to `slicer.user_dir`; a preset whose parent is in neither the bundled
profiles nor the same import is refused, and a process's `post_process` (a command Orca would run on the warden's
host) is dropped. `klipper` reads the bed from the stepper limits, the height, nozzle, kinematics, speed and
acceleration limits and the `PRINT_START`/`START_PRINT` and `PRINT_END`/`END_PRINT` macros (with the parameter names
the macro reads, e.g. `BED_TEMP` / `EXTRUDER_TEMP`); it inherits Orca's generic Klipper printer for that nozzle, so its
processes and Orca's universal filaments apply. Without a start macro it heats, homes and draws a purge line; check that
start gcode before the first print. Then run `print-warden check`.

Some bundled start gcode purges outside the printable area (the Anycubic Kobra 2 Pro purges at Y226 on a 220 mm
bed). The part must always extrude on the printable area; the start gcode's purge may also use the printer's
`purge_zone` (`[x0, y0, x1, y1]` in bed mm, a strip the head reaches). Without one, the job is blocked and the
blocker suggests the zone to set: check that the printer really reaches it before setting it.

`print-warden profiles diff` takes a snapshot of every preset printers.yaml uses, flattened; run it again after an
OrcaSlicer upgrade to see each setting that changed per printer, then `--accept` the new snapshot.
`print-warden check` warns while the presets differ from the snapshot. `check` also validates `THINGIVERSE_TOKEN`
when it is set, and with `--sites` that Printables still answers the warden's query.

## Run

```
cp printers.example.yaml printers.yaml   # edit
printf 'HA_BEARER_TOKEN=...\nOBICO_WEBHOOK_SECRET=...\nPRINT_WARDEN_TOKEN=...\nHA_ACTION_SECRET=...\n' > .env
docker compose run --rm --build print-warden print-warden check   # every profile, service and camera answers
docker compose up -d --build             # Ubuntu 24.04 + OrcaSlicer 2.4.2 AppImage + OpenSCAD + ffmpeg
curl localhost:8720/healthz
```

Released images: `ghcr.io/moellere/print-warden:<version>`. Operating, backups and troubleshooting:
`docs/operations.md`; releases: `docs/releasing.md`; changes: `CHANGELOG.md`.

Access:

| Route | Guard |
|---|---|
| `/mcp`, `/upload`, `/status`, `/stage` | `PRINT_WARDEN_TOKEN` as `Authorization: Bearer …` or `?token=…` (open, with a startup warning, if unset) |
| `/obico/webhook` | `?secret=$OBICO_WEBHOOK_SECRET`; disabled if unset |
| `/ha/action` | `?secret=$HA_ACTION_SECRET`; disabled if unset |
| `/files/{snapshots,timelapse,jobs}/…` | open, so the phone can load notification images; databases and uploaded models are never served |
| `/healthz` | open |

`/status` (open it once with `?token=…` and bookmark it) is one read-only page for the house: per printer its state,
progress and finish time, bed, queue with estimates, latest snapshot and maintenance due, then filament to reorder or
dry. It refreshes every 30 s and has no buttons; starts, cancels and bed-clears stay on the phone.

`/stage` (also opened with `?token=…`) adds a print from a browser: a file or a link, where, material, quality,
copies and 3MF plate. Slice shows the preview, estimate, warnings and blockers; **Add to queue** puts it in the
printer's queue, where it is offered on the phone like any job, and **Discard** drops it. The page never starts,
cancels or waives anything, and its buttons only act on jobs staged from it. Forms carry a CSRF token (valid for up to
two hours) and must be posted from the page's own origin.

Snapshots and timelapse frames older than `storage.keep_days` are deleted daily along with old events; rendered
timelapses, job artifacts and uploads are kept.

Obico → Preferences → Notifications → Webhook URL: `http://<host>:8720/obico/webhook?secret=<OBICO_WEBHOOK_SECRET>`.
Phone buttons come back through HA's websocket event bus (`mobile_app_notification_action`), no HA config needed.

## Layout

```
src/print_warden/
  config.py     YAML -> dataclasses; secrets from env
  moonraker.py  websocket subscriber (read) + upload/start (write, policy-gated)
  rules.py      telemetry rules -> events
  watcher.py    per-printer orchestration, snapshots, light, timelapse, job tracking
  slicer.py     OrcaSlicer CLI wrapper, preset lookup and flattening, gcode header parsing, OpenSCAD render
  jobs.py       job ledger (SQLite): transition table, transition log
  policy.py     can_start(), approval_expired()
  reconcile.py  settle jobs from Moonraker history after a restart or outage
  retention.py  snapshot / frame pruning
  timelapse.py  frames -> mp4 (ffmpeg)
  bedcheck.py   empty-bed comparison + bed ledger (state, labelled checks)
  stats.py      estimate accuracy, ETA correction, failure rates, monthly usage
  ha_entities.py  per-printer HA sensors (REST states API)
  checks.py     slice checks: model fit, gcode extents, temperatures, blocking Orca warnings
  preview.py    job preview drawn from the gcode (top-down extrusion on the bed outline)
  park.py       bed-check pose: end-gcode park at slice time, pose test, opt-in Y rehome
  ha.py         alert webhook, actionable notifications, light control
  ha_events.py  HA websocket: notification button taps
  spoolman.py   Spoolman client (materials, remaining weight)
  threemf.py    3MF plates to STL, project sanitizing (no project gcode reaches the printer)
  footprint.py  bed-camera homography, first-layer footprint, part-on-bed verdict
  calibration.py  temperature / flow / pressure-advance towers and their results
  intake.py     model pages (Thingiverse, Printables, any page) -> files, title, author, license
  status.py     the read-only /status page
  stage.py      the /stage page: slice and queue from a browser, never start
  profiles.py   Orca vendor listing, slicer blocks, preset import, profiles from Klipper
  server.py     MCP tools + HTTP routes (/upload, /status, /stage, /obico/webhook, /ha/action, /files, /healthz)
```

## Develop

```
uv sync --group dev
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q
```

CI runs the same four on every push and pull request.
