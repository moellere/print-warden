# Stability

What print-warden promises about each surface someone else builds on: your config, your Home Assistant automations,
an MCP client, homelab-helper, a script. `tests/test_stability_doc.py` holds the tables below to the code.

## Promise levels

- **Stable**: from 1.0, it is not removed or renamed within a major version. Fields, arguments and keys may be
  *added* in any release; an added argument is always optional.
- **Internal**: free to change in any release. Nothing outside the warden should depend on it.

## Deprecation policy

A stable surface that has to go is deprecated first: the release that deprecates it keeps it working, says so in
the CHANGELOG, and (for config keys and MCP arguments) logs a warning when it is used. It is removed no earlier than
the next major version. Before 1.0 (now), the CHANGELOG's upgrade notes say what changed; from 1.0 this policy holds.

## Safety invariants (stable, and not up for deprecation)

- A print starts only on a phone tap that answers a request still inside the approval TTL, on `start_print` with
  the operator's quoted consent, or, where `auto_start` is set, when the bed check and every start check pass.
  No tool, page or verb starts one any other way.
- The deterministic start policy (`policy.can_start`) decides every start; no model takes part in a decision.
- Cancelling or resuming a running print is a human's tap on a warden notification. The one automatic cancel is a
  pause left unanswered past `policy.paused_cancel_minutes`.
- Secrets come from the environment, never from the config file or storage.

## CLI verbs (stable)

| Verb | What |
|---|---|
| `print-warden` | run the warden |
| `check` | check a config without starting the warden (`[config] [--sites]`) |
| `profiles` | find, import or generate printer profiles |
| `validate` | run the live-validation runbook against the running warden |
| `demo` | run the warden against simulated printers |
| `backup` | write the warden's state to one archive |
| `restore` | put a backup's state back |

Each verb's flags are stable as documented in its `--help`; output text is internal.

## MCP tools (stable)

Names and argument names; `?` marks an optional argument. Result fields are only ever added.

| Tool | Arguments |
|---|---|
| `batch_status` | `batch_id` |
| `bed_status` | `printer` |
| `calibrate` | `printer`, `kind`, `quality?`, `bowden?` |
| `calibrate_bed_camera` | `printer`, `corners`, `calibrated_by` |
| `capture_bed_reference` | `printer`, `authorized_by` |
| `check_bed` | `printer` |
| `delete_saved_batch` | `name` |
| `fault_test` | `printer`, `requested_by` |
| `footprint_check` | `job_id` |
| `get_events` | `printer?`, `since_minutes?`, `limit?`, `min_severity?` |
| `get_snapshot` | `printer?`, `camera?` |
| `job_status` | `job_id` |
| `label_print` | `job_id`, `result`, `labelled_by`, `reason?` |
| `list_jobs` | `printer?`, `limit?` |
| `list_printers` | — |
| `list_saved_batches` | — |
| `list_spools` | — |
| `loaded_material` | `printer` |
| `mark_bed_clear` | `printer`, `authorized_by` |
| `model_history` | `model` |
| `model_page` | `url` |
| `move_job` | `job_id`, `position` |
| `offer_next` | `printer` |
| `print_history` | `printer?`, `days?`, `state?` |
| `print_status` | `printer?` |
| `printer_health` | `printer`, `accept?` |
| `printer_stats` | `days?` |
| `queue` | `printer` |
| `queue_saved_batch` | `name`, `where?` |
| `record_calibration` | `job_id`, `section`, `recorded_by` |
| `record_maintenance_done` | `printer`, `item`, `done_by` |
| `record_spool_dried` | `spool_id`, `done_by` |
| `register_spool` | `vendor`, `material`, `color`, `registered_by`, `net_weight_g?`, `spool_weight_g?`, `price?`, `location?`, `name?`, `diameter_mm?`, `density?` |
| `reprint` | `job_id`, `requested_by`, `offer?` |
| `request_start` | `job_id` |
| `retire_spool` | `spool`, `retired_by` |
| `save_batch` | `name`, `where`, `items` |
| `send_alert` | `printer`, `message`, `severity?` |
| `set_active_spool` | `printer`, `spool_id` |
| `skip_job` | `job_id`, `reason?` |
| `start_print` | `job_id`, `authorized_by` |
| `submit_batch` | `where`, `items`, `name?`, `save_as?`, `use_spool_temps?` |
| `submit_job` | `printer`, `model`, `material`, `quality?`, `name?`, `overrides?`, `scad_defines?`, `copies?`, `orient?`, `offer?`, `use_spool_temps?`, `plate?`, `file?` |
| `submit_split` | `where`, `model`, `material`, `copies`, `quality?`, `name?`, `overrides?`, `scad_defines?`, `orient?`, `use_spool_temps?`, `plate?`, `file?` |
| `swap_spool` | `printer`, `spool`, `swapped_by`, `outgoing_gross_g?` |
| `upkeep_status` | — |
| `waive_blockers` | `job_id`, `authorized_by` |
| `weigh_spool` | `spool`, `gross_g`, `weighed_by`, `empty_g?` |

## HTTP routes (stable)

| Route | What |
|---|---|
| `/mcp` | MCP, Streamable HTTP |
| `/upload` | model upload (multipart `file`) |
| `/status` | the read-only status page |
| `/stage` | the staging page |
| `/spools` | the spool inventory page |
| `/healthz` | the status contract below |
| `/obico/webhook` | Obico's webhook |
| `/ha/action` | notification taps posted by Home Assistant |
| `/files/snapshots`, `/files/timelapse`, `/files/jobs` | snapshots, timelapses and job artifacts |

The pages' HTML is internal; their URLs and the token rule are stable.

## `/healthz` (stable, api 1)

Top level:

| Field |
|---|
| `ok` |
| `api` |
| `version` |
| `printers` |

Per printer:

| Field |
|---|
| `connected` |
| `klippy` |
| `state` |
| `progress_pct` |
| `finishes_in_s` |
| `bed` |
| `queue` |
| `last_update_s_ago` |
| `last_shutdown` |
| `outcomes_7d` |
| `maintenance_due` |
| `health` |

Fields are only added within api 1. A change that removes or changes the meaning of one raises `api`.

## Config (stable)

Every key `config.SCHEMA` lists, by section. `print-warden check` warns on a key it does not know, with the closest
known key. The server also logs a warning at start.

| Section |
|---|
| `printers` |
| `home_assistant` |
| `obico` |
| `storage` |
| `server` |
| `slicer` |
| `spoolman` |
| `intake` |
| `policy` |
| `alerts` |
| `presence` |
| `energy` |
| `upkeep` |

`printers.example.yaml` shows them in use.

## Home Assistant entities and MQTT (stable)

Per printer, `sensor.print_warden_<printer>_<suffix>` (the printer's name lowercased, runs of other characters as
`_`):

| Suffix |
|---|
| `state` |
| `progress` |
| `finish` |
| `queue` |
| `bed` |
| `material` |
| `maint_<item>` |

With MQTT, the same rows publish under `print_warden/<printer>/<suffix>/state` and `…/attributes`, with discovery
under the configured prefix and availability at `print_warden/status`.

## Notification actions (stable)

The `action` of every button the warden sends is `PRINT_WARDEN_<VERB>_<argument>`. Automations may watch for these.

| Verb |
|---|
| `START` |
| `SKIP` |
| `BEDCLEAR` |
| `FIRSTLAYER` |
| `OUTCOME` |
| `WEIGH` |
| `REPRINT` |
| `CALIB` |
| `MAINT` |
| `DRIED` |
| `CANCEL` |
| `RESUME` |

Notification titles and wording are internal.

## Storage and backups (stable format, internal contents)

`print-warden backup` writes a `.tar.gz` that `print-warden restore` of the same or a later version reads. It holds
`jobs.db`, `events.db`, `bed.db`, `validation.json`, the warden's `profiles/`, the empty-bed `reference/` images and
a `manifest.json` (`version`,
`created`, `files`). The database schemas are internal. A newer warden only ever adds columns to them, so an older
database is upgraded in place when it opens.

## Internal

Python modules and their functions, the database schemas, log text, notification and page wording, `sim.py` and
`demo.py`'s simulation (`slicer.orca: sim` is stable as a config value, the gcode it writes is not).
