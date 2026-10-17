# Changelog

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
