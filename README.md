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
| Cancel a running print, power off | a human |

Printers with a `power_device` (the X1's Sonoff) are powered on at start and Klipper is coaxed to ready with
up to three FIRMWARE_RESTARTs before the print command goes out; the tap still gates the whole sequence.

`policy.can_start` is plain Python: Moonraker connected, Klipper ready, printer idle, gcode staged,
loaded material matches the job, and the active spool has the job's grams plus 10%. No model is consulted in the gate.
A phone tap answers one `request_start` and is honoured only within `policy.approval_ttl_minutes` (default 15);
unanswered requests go back to `staged` and the notification is withdrawn. Starts on one printer are serialised, so a
double tap sends one print.

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

`auto_start: true` starts a newly staged job without a tap only when the bed is marked clear, a live check reads clear,
the last `min_agreements` labelled checks all agreed with the operator, and the local time is inside
`auto_start_hours`. Each unattended start sends an alert with the check's snapshot. A missed small part (a false
"clear" after a print) breaks the record, so auto_start stops until the check earns it back.

## Job flow

```
curl -F file=@part.stl http://nas.local:8710/upload           # or a .scad
submit_job(printer="garagex4", model="part.stl", material="PETG", quality="standard")
   -> slices (tree supports, no brim), uploads gcode to Moonraker as warden/<job>_<name>_<mat>.gcode
job_status(job_id)          # estimates, warnings, can_start + blockers
request_start(job_id)       # phone notification with Start / Skip buttons
start_print(job_id, authorized_by="the operator said: go ahead and print it")   # chat path
list_spools() / set_active_spool("garagex4", 2)   # Spoolman owns spools; the warden records which one is loaded
```

When a print ends the warden deducts the filament Klipper reports from the job's spool in Spoolman (set
`track_spool_usage: false` on a printer whose Moonraker runs its own `[spoolman]` component), renders the per-layer
frames into `timelapse.mp4`, and sends the completion alert with actual vs. estimated time, grams and the timelapse link.
On every Moonraker (re)connect, jobs whose outcome happened while the warden was away are settled from Moonraker's
print history.

Qualities: draft (0.24), standard (0.20), strength (0.20), fine (0.12). Materials: PLA, PETG, ABS.
Models are STL or SCAD, up to 100 MB.
`overrides` on `submit_job` are OrcaSlicer process keys applied last (e.g. `{"enable_support": "0"}`).

## Run

```
cp printers.example.yaml printers.yaml   # edit
printf 'HA_BEARER_TOKEN=...\nOBICO_WEBHOOK_SECRET=...\nPRINT_WARDEN_TOKEN=...\nHA_ACTION_SECRET=...\n' > .env
docker compose up -d --build             # Ubuntu 24.04 + OrcaSlicer 2.4.2 AppImage + OpenSCAD + ffmpeg
curl localhost:8710/healthz
```

Access:

| Route | Guard |
|---|---|
| `/mcp`, `/upload` | `PRINT_WARDEN_TOKEN` as `Authorization: Bearer …` or `?token=…` (open, with a startup warning, if unset) |
| `/obico/webhook` | `?secret=$OBICO_WEBHOOK_SECRET`; disabled if unset |
| `/ha/action` | `?secret=$HA_ACTION_SECRET`; disabled if unset |
| `/files/{snapshots,timelapse,jobs}/…` | open, so the phone can load notification images; databases and uploaded models are never served |
| `/healthz` | open |

Snapshots and timelapse frames older than `storage.keep_days` are deleted daily along with old events; rendered
timelapses, job artifacts and uploads are kept.

Obico → Preferences → Notifications → Webhook URL: `http://<host>:8710/obico/webhook?secret=<OBICO_WEBHOOK_SECRET>`.
Phone buttons come back through HA's websocket event bus (`mobile_app_notification_action`), no HA config needed.

## Layout

```
src/print_warden/
  config.py     YAML -> dataclasses; secrets from env
  moonraker.py  websocket subscriber (read) + upload/start (write, policy-gated)
  rules.py      telemetry rules -> events
  watcher.py    per-printer orchestration, snapshots, light, timelapse, job tracking
  slicer.py     OrcaSlicer CLI wrapper, preset flattening, gcode header parsing, OpenSCAD render
  jobs.py       job ledger (SQLite): transition table, transition log
  policy.py     can_start(), approval_expired()
  reconcile.py  settle jobs from Moonraker history after a restart or outage
  retention.py  snapshot / frame pruning
  timelapse.py  frames -> mp4 (ffmpeg)
  bedcheck.py   empty-bed comparison + bed ledger (state, labelled checks)
  park.py       bed-check pose: end-gcode park at slice time, pose test, opt-in Y rehome
  ha.py         alert webhook, actionable notifications, light control
  ha_events.py  HA websocket: notification button taps
  spoolman.py   Spoolman client (materials, remaining weight)
  server.py     MCP tools + HTTP routes (/upload, /obico/webhook, /ha/action, /files, /healthz)
```

## Develop

```
uv sync --group dev
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src && uv run pytest -q
```

CI runs the same four on every push and pull request.
