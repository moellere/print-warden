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
| Start a print | the operator: a phone tap (`request_start`) or quoted chat permission (`start_print`), and `policy.can_start` must pass |
| Pause on detected failure | Obico |
| Cancel a running print, power off | a human |

Printers with a `power_device` (the X1's Sonoff) are powered on at start and Klipper is coaxed to ready with
up to three FIRMWARE_RESTARTs before the print command goes out; the tap still gates the whole sequence.

`policy.can_start` is plain Python: Moonraker connected, Klipper ready, printer idle, gcode staged,
loaded material matches the job. No model is consulted in the gate.

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

Qualities: draft (0.24), standard (0.20), strength (0.20), fine (0.12). Materials: PLA, PETG, ABS.
`overrides` on `submit_job` are OrcaSlicer process keys applied last (e.g. `{"enable_support": "0"}`).

## Run

```
cp printers.example.yaml printers.yaml   # edit
printf 'HA_BEARER_TOKEN=...\nOBICO_WEBHOOK_SECRET=...\n' > .env
docker compose up -d --build             # Ubuntu 24.04 + OrcaSlicer 2.4.2 AppImage + OpenSCAD
curl localhost:8710/healthz
```

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
  jobs.py       job ledger (SQLite) with transition log
  policy.py     can_start()
  ha.py         alert webhook, actionable notifications, light control
  ha_events.py  HA websocket: notification button taps
  spoolman.py   Spoolman client (materials, remaining weight)
  server.py     MCP tools + HTTP routes (/upload, /obico/webhook, /ha/action, /files, /healthz)
```
