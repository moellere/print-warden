# Operating print-warden

## Deploy

```
cp printers.example.yaml printers.yaml                     # edit: printers, cameras, presets, HA, Spoolman
printf 'HA_BEARER_TOKEN=...\nOBICO_WEBHOOK_SECRET=...\nPRINT_WARDEN_TOKEN=...\nHA_ACTION_SECRET=...\n' > .env
docker compose run --rm print-warden print-warden check   # 0 failed before going further
docker compose up -d                                       # builds locally, or set image: in compose.yml
curl -s localhost:8720/healthz | jq
```

To run a released image instead of building, replace `build: .` in `compose.yml` with
`image: ghcr.io/moellere/print-warden:v0.4.0` (or `:latest`).

MCP clients connect to `http://<host>:8720/mcp` with `Authorization: Bearer $PRINT_WARDEN_TOKEN`, or
`?token=...` for clients that only take a URL.

## First-run checklist

`print-warden check [printers.yaml]` resolves every Orca profile and asks Moonraker, the cameras, Home Assistant
(token, notify service, lights, quiet entity), Spoolman and the MQTT broker whether they answer; it changes nothing
and exits 1 if anything fails. Then walk `docs/live-validation.md` for what only a real printer can show.

- `list_printers` shows every printer `moonraker_connected: true` and the material you expect.
- `set_active_spool` for each printer if you use Spoolman.
- Bed check (if configured): park the bed at `bed_check.pose` (a warden-sliced print ends there), confirm the bed is
  empty, then tap "Bed cleared" once to capture the reference, or `capture_bed_reference`. Watch `check_bed` scores
  for a few prints before turning on `auto_start`.
- Home Assistant: the `sensor.print_warden_*` entities appear within 30 s of start-up.

## Data and backups

Everything lives under `storage.dir` (`./data` in compose):

| Path | What | Back up? |
|---|---|---|
| `jobs.db` | job ledger: states, logs, outcomes, history | yes |
| `bed.db` | bed state and labelled bed checks (auto_start's record) | yes |
| `events.db` | events, pruned after `keep_days` | optional |
| `reference/` | empty-bed reference images | yes |
| `jobs/` | per-job STL, gcode, 3mf, preview | optional |
| `models/` | uploaded models | optional |
| `slice-cache/`, `snapshots/`, `timelapse/` | regenerable or pruned | no |

Stop the container (or copy with `sqlite3 jobs.db ".backup ..."`) before copying the databases.

## Upgrade

Pull or rebuild, then `docker compose up -d`. Schema changes are additive and applied at start-up (new columns are
added to existing databases). Read `CHANGELOG.md` first: some releases change defaults (0.2.0 adds
`curr_bed_type: High Temp Plate` to the house rules and blocks starts until the bed is marked clear after a print).

## Troubleshooting

| Symptom | Look at |
|---|---|
| A start is refused | `job_status(job_id)` → `blockers`; slice blockers need `waive_blockers` with the operator's words |
| The queue offers nothing | `offer_next(printer)` returns `why` (bed not cleared, request waiting, printer busy) |
| Bed check skipped | `bed_status` → `at_pose`; the bed must be homed at `bed_check.pose` |
| No phone buttons | HA notify service name; the HA websocket must be connected (warden log "subscribed to HA notification actions") |
| Estimates look off | `printer_stats` → `estimate_accuracy` and `eta_correction` |
| A printer shows offline | `/healthz` → `connected`, `last_update_s_ago`; Moonraker URL and network |

## Status contract

`GET /healthz` (no token) returns, with `"api": 1`, the version and per printer `connected`, `klippy`, `state`,
`progress_pct`, `finishes_in_s`, `bed`, `queue` and `last_update_s_ago`, and since 0.4:

- `last_shutdown`: `{cause, hint, s_ago}` for a Klipper shutdown in the last week that Klipper has not been ready
  since. `cause` is the classification (`thermal_runaway`, `heater_not_heating`, `thermistor_out_of_range`,
  `mcu_comm_lost`, `mcu_timing`, `tmc_driver_error`, ..., `unknown`) and `hint` its fixed advice; never the raw
  message.
- `outcomes_7d`: `{prints, failed}`, with labels winning over how the job ended.
- `maintenance_due`: the names of the printer's maintenance items that are due.

`ok` is true when every printer is connected. Fields are only added within an api version; it never carries file
names, job names or URLs. homelab-helper's `printwarden` source turns these into findings.
