# Live validation

The test suite runs against fakes: a fake Moonraker, a fake Home Assistant, a fake broker. This runbook is where the
warden meets the real printers, cameras, phone and Orca. Run it after a deploy that changes behaviour, and sign off
each row per printer (date and initials, or an issue link when it fails).

Before anything else, `print-warden check` (in the container: `docker compose exec print-warden print-warden check`)
must report `0 failed`. It confirms the profiles resolve and that Moonraker, the cameras, Home Assistant, Spoolman
and the broker answer, without printing or publishing anything.

Use a small, fast model (a 20 mm calibration cube in PLA at `draft`) unless a row says otherwise.

## 1. Slice and stage

1. `submit_job(printer, model="cube.stl", material="PLA", quality="draft", offer=False)`.
   Expect: estimates, a preview URL that opens, no blockers, the job `staged`.
2. Upload a `.3mf` exported from Orca, Bambu Studio and PrusaSlicer (one each, at least one with two plates);
   `submit_job(..., plate=2)`. Expect: the plate's objects only, a warning naming the plates, no project gcode in
   the job's gcode (its start gcode is the printer profile's).
3. A model larger than the bed. Expect: refused before slicing, with the axis that does not fit.

## 2. Start, watch, finish

4. `request_start` → the phone gets Start / Skip. Tap **Start**. Expect: the print starts, the light comes on.
5. Tap an old Start notification after the approval TTL. Expect: refused, the job back to `staged`.
6. At layer 2: the first-layer prompt arrives with the nozzle-camera frame. Tap **Looks good**.
7. At the end: the completion alert with actual vs. estimated time and grams; Spoolman's spool lighter by about
   the job's grams; the timelapse link opens; the printer parks at `bed_check.pose` with steppers held.
8. The outcome prompt arrives with the end photo. Tap **Failed** → **More…** → **Layer shift**; `print_history`
   shows `outcome: failed, outcome_reason: layer_shift`. Re-submit the same model and settings: the job carries the
   "failed before" warning.
9. `sensor.print_warden_<printer>_*` (or the MQTT device) shows printing → progress → finish → standby.

## 3. Bed

10. After a print the bed is `occupied`; `request_start` for the next job is blocked.
11. Remove the part, tap **Bed cleared**. Expect: a labelled check recorded; the next queued job is offered.
12. Leave the part on and run `check_bed`. Expect: not clear, with the changed fraction.

## 4. Stop and recover

13. Mid-print, tap **Cancel print** on the first-layer prompt. Expect: cancelled; the job `failed` (print_cancelled) and the reasons prompt arrives; a stale
    Cancel button from an earlier job does nothing.
14. Pause from Obico (or `PAUSE` in the console with Obico watching). Expect: Resume / Cancel on the phone;
    **Resume** resumes.
15. Restart the warden mid-print. Expect: the job still `printing` after reconnect; on finish it is settled from
    Moonraker's history.

## 5. Alerts

16. Inside `quiet_hours`, or with `quiet_entity` in a quiet state: a warning (unplug a fan's tach or set a low
    `temp_limits` value) is held; a critical alert still arrives.
17. Toggle `quiet_entity` off. Expect: the next warning arrives.

## 6. Integrations

18. With `home_assistant.mqtt`: the device appears under MQTT with six sensors; rename one; restart HA; the
    rename and the states survive. Stop the warden: the entities go unavailable.
19. homelab-helper: `helper discover printwarden --persist` lists the printers; power one off and re-run; a
    `printer-offline` finding opens; power it on and re-run; it resolves.

## Sign-off

| # | Row | garagex4 | wyolax1 |
|---|---|---|---|
| 0 | `print-warden check` reports 0 failed | | |
| 1 | Slice and stage an STL | | |
| 2 | 3MF from Orca / Bambu Studio / PrusaSlicer, plate 2 | | |
| 3 | Oversized model refused before slicing | | |
| 4 | Start by tap | | |
| 5 | Expired Start tap refused | | |
| 6 | First-layer prompt | | |
| 7 | Completion: alert, Spoolman, timelapse, park | | |
| 8 | Outcome prompt labels the job; "failed before" warning | | |
| 9 | HA entities follow the print | | |
| 10 | Occupied bed blocks the next start | | |
| 11 | Bed cleared → labelled check → next job offered | | |
| 12 | Bed check sees a part | | |
| 13 | Cancel by tap; stale Cancel ignored | | |
| 14 | Obico pause → Resume | | |
| 15 | Restart mid-print, settled from history | | |
| 16 | Quiet hours / quiet entity hold warnings, not criticals | | |
| 17 | Quiet entity off → warnings resume | | |
| 18 | MQTT device, rename survives HA restart, unavailable when stopped | | |
| 19 | homelab-helper `printer-offline` opens and resolves | | |
