# Operating print-warden

## Deploy

```
cp printers.example.yaml printers.yaml                     # edit: printers, cameras, presets, HA, Spoolman
printf 'HA_BEARER_TOKEN=...\nOBICO_WEBHOOK_SECRET=...\nPRINT_WARDEN_TOKEN=...\nHA_ACTION_SECRET=...\n' > .env
docker compose run --rm print-warden print-warden check   # 0 failed before going further
docker compose up -d                                       # builds locally, or set image: in compose.yml
curl -s localhost:8710/healthz | jq
```

To run a released image instead of building, replace `build: .` in `compose.yml` with
`image: ghcr.io/moellere/print-warden:v0.4.0` (or `:latest`).

MCP clients connect to `http://<host>:8710/mcp` with `Authorization: Bearer $PRINT_WARDEN_TOKEN`, or
`?token=...` for clients that only take a URL.

`.env` lives on the Docker host, next to `compose.yml`; `env_file` hands its values to the container as environment
variables, so there is no `.env` inside the container. `docker compose exec print-warden printenv PRINT_WARDEN_TOKEN`
shows what the warden got.

## Signing in from a browser, and the inbox

The pages a person uses (`/status`, `/stage`, `/spools`, `/inbox`) take a sign-in instead of the token:

1. Add `PRINT_WARDEN_OPERATOR_PASSWORD=<a password>` to `.env` and `docker compose up -d` (a restart picks it up).
   `server.operator_password_env` renames the variable.
2. Open `http://<host>:8710/inbox` on the phone or a laptop and sign in. The browser stays signed in for 30 days.
3. From then on a tap on a warden notification's body opens the inbox, and `/status` links to it.

The password is the operator's, not a machine's: keep it out of MCP client configs and scripts, which use the
token. The token alone opens `/status`, `/stage` and `/spools` as before, but never the inbox's buttons. The
inbox is not served on the public hostname (`server.oauth` passes only `/mcp`); reach it on the LAN or a VPN.
Five wrong passwords lock sign-in for 10 minutes, and each is logged with the client's address.

## Remote access

Hosted MCP clients (the Claude apps, for one) connect from the vendor's servers, so they need a public HTTPS URL
and OAuth. The warden does not run an authorization server; it checks tokens from one you already have.

1. **Identity provider.** Create an OAuth2/OIDC application (authorization code + PKCE) whose access tokens are
   JWTs signed with RS256 or ES256 and carry the user's groups. Allow the client's redirect URI (for Claude:
   `https://claude.ai/api/mcp/auth_callback`). Put each person in an operator group or a queue group.
2. **Public hostname.** Route a hostname on port 443 to the warden (a tunnel or reverse proxy). Everything may be
   routed: for that hostname the warden serves only `/mcp` and `/.well-known/oauth-protected-resource`.
3. **Config.**

   ```yaml
   server:
     oauth:
       issuer: https://id.example.com/application/o/print-warden/
       resource: https://prints.example.com/mcp
       audience: the-oauth-client-id
       operator_groups: [print-operators]
       queue_groups: [print-family]
   ```

   `issuer` and `audience` must equal the token's `iss` and `aud`. `jwks_url` is read from the issuer's
   `/.well-known/openid-configuration` unless set. `scopes` (default `openid`, `profile`) is what clients are told
   to ask for, and `groups_claim` (default `groups`) is where the token lists groups.
4. **Client.** Add `resource` as a custom connector. If the provider cannot register clients by itself, enter the
   client ID (and secret, if it issued one) by hand.

What a remote caller gets:

| Role | Tools |
|---|---|
| operator | all of them |
| queue | the read tools, `submit_job`, `submit_split`, `submit_batch`, `save_batch`, `queue_saved_batch`, `reprint`, `request_start`, `offer_next` |

A queue-role caller can ask for a start (`request_start` puts the Start button on the operator's phone) but cannot
start one, waive blockers, clear the bed or edit spools. Each remote tool call is logged with the account's name.
An account in neither group is refused with 403. Remote callers cannot upload a file; they print from a model page
or a URL.

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
| `profiles/` | imported and generated printer presets (`print-warden profiles`) | yes |
| `slice-cache/`, `snapshots/`, `timelapse/` | regenerable or pruned | no |

`print-warden backup` (in the container: `docker compose exec print-warden print-warden backup --out
/data/backup.tar.gz`) writes every "yes" row above, plus `events.db` and `validation.json`, to one archive. It takes
consistent copies of the databases, so the warden may keep running. `print-warden restore <archive>` puts them back
once the warden is stopped: `docker compose run --rm print-warden print-warden restore /data/backup.tar.gz`. It keeps
each file it replaces as `<name>.before-restore-<time>`. A newer warden reads an older backup; its databases are
upgraded in place on start.

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
  message. `expected` is true when the shutdown happened with the printer's `power_device` off (a USB-backfed
  board losing its link is normal then); monitors should not treat that one as a fault.
- `outcomes_7d`: `{prints, failed}`, with labels winning over how the job ended.
- `maintenance_due`: the names of the printer's maintenance items that are due.
- `health`: the kinds of printer-health finding from the last hourly check (`config_changed`,
  `save_config_pending`, `firmware_mismatch`, `update_available`, `mesh_drift`); never the details, which can name
  files. `printer_health` over MCP has them.

`ok` is true when every printer is connected. Fields are only added within an api version; it never carries file
names, job names or URLs. homelab-helper's `printwarden` source turns these into findings.
