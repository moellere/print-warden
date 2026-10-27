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
   the job's gcode (its start gcode is the printer profile's). One with a negative part (a hole cut by a volume):
   the hole is in the print.
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
    With `calibrate_bed_camera` done: `footprint_check(job_id)` with the part on reads no hint; take the part off
    and run it again: "probably failed: 1 of 1 part(s) not seen".

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

18. With `home_assistant.mqtt`: the device appears under MQTT with six sensors (plus one per maintenance item); rename one; restart HA; the
    rename and the states survive. Stop the warden: the entities go unavailable.
19. homelab-helper: `helper discover printwarden --persist` lists the printers; power one off and re-run; a
    `printer-offline` finding opens; power it on and re-run; it resolves.
25. Force a shutdown (`M112` in the console): `/healthz` shows `last_shutdown.cause`; `helper discover printwarden
    --persist` opens a Klipper finding naming the cause; `FIRMWARE_RESTART` and re-run: it resolves.

## 7. Upkeep

20. Set a maintenance item to `{every_hours: 0.1}` and finish a print: within the hour the phone gets "<item> is
    due" with **Done**; tap it; `upkeep_status` shows it reset and the `maint_<item>` sensor reads 0.
21. Set `upkeep.reorder_below_g` above a material's stock: the reorder alert arrives once, and not again that day.
22. A loaded TPU spool first used more than `dry_after_days` ago: the drying reminder; **Dried** resets it.

## 8. Scheduling

23. `submit_split("<location>", "cube.stl", "PLA", 6)` with both printers idle and PLA loaded: two jobs, about three
    copies each; each is offered on its own printer and starts only on its tap.
24. With `policy.cheap_power` and the price above `max_price`: a long job is not offered (`advance_queue` says why);
    when the price drops, it is offered within the hour.

35. `submit_batch("<location>", [{"model": "<4-plate project>.3mf", "material": "PLA", "plates": "all"}],
    name="test batch")` with both printers idle and PLA loaded. Expect: four jobs across the two printers, the
    longest plate on the faster printer; each starts only on its tap; `/status` shows "test batch: n of 4 done".

## 9. Calibration

26. `set_active_spool`, then `calibrate(printer, "temp")`; tap Start. Expect: the tower changes temperature at 8, 16,
    24 and 32 mm (the console shows `M104`), the phone asks which section looked best; pick one; Spoolman shows
    `pw_nozzle_temp` on the spool and the next job's warnings say its nozzle temperature came from the spool.
27. `calibrate(printer, "flow")` then a normal print: `M221` is back at 100 % (`M221` without arguments in the
    console reports it).

## 10. Status page and model pages

28. Open `http://<host>:8710/status?token=…` on a phone and a desktop while one printer prints. Expect: both printers,
    the printing one with progress and a finish time, the queue with estimates, the latest snapshot; without the
    token, 401.
29. `model_page("https://www.thingiverse.com/thing:<id>")` with `THINGIVERSE_TOKEN` set, then
    `submit_job(printer, <same url>, "PLA", file=<one of them>)`. Expect: the file is the one named and
    `print_history` shows the page, author and license.
30. The same with a `https://www.printables.com/model/<id>-…` page. If it fails with "unofficial", Printables changed
    its API: open an issue with the error.

## 11. Printer profiles

31. On the warden host, `print-warden profiles klipper "<name>" http://<printer>:7125`, paste the printed block into a
    test printer entry (or replace the slicer block of one printer in a copy of printers.yaml), `print-warden check`,
    then slice and print a 20 mm cube from it. Expect: the bed size and height in the written preset match
    `printer.cfg`, the gcode starts with your `PRINT_START` macro and its temperatures, and the cube prints.
32. Export a printer or filament preset from OrcaSlicer (File → Export → Export Preset Bundle), then
    `print-warden profiles import <bundle>` and slice with the printed block. Expect: the job's gcode carries your
    preset's settings (e.g. its nozzle temperature).

## 12. Checks and profile changes

33. `print-warden profiles diff` (takes the snapshot), then after the next OrcaSlicer image upgrade run it again.
    Expect: either "no changes" or the changed settings per printer; `print-warden check` warns until `--accept`.
34. `print-warden check --sites`. Expect: `thingiverse` ok when `THINGIVERSE_TOKEN` is set, `printables` ok.

## 13. Staging page

36. On a phone, open `http://<host>:8710/stage?token=…`, upload an STL, pick a printer, PLA, standard; Slice. Expect:
    the preview and estimate within a minute. Add to queue: the phone gets the Start offer (or, if the printer is busy,
    the page says it is queued). A second form opened in another tab and posted after two hours is refused.

## 14. Energy

37. Set `power_entity` on one printer (an HA energy counter on its plug, or a power sensor), `print-warden check`
    (the entity reads as an energy or power sensor), then print. Expect: `print_history` shows `energy_kwh` within a
    few percent of the counter's rise in HA over the print, `heatup_kwh` well under it, and an `energy_cost`.

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
| 20 | Maintenance due → Done resets it and the sensor | | |
| 21 | Reorder alert once a day | | |
| 22 | Drying reminder → Dried resets it | | |
| 23 | submit_split shares copies; each share tap-started | | |
| 24 | Cheap-power hold, offered when the price drops | | |
| 25 | Shutdown cause in `/healthz` and as a homelab-helper finding | | |
| 26 | Temperature tower → section saved to the spool and used | | |
| 27 | Flow tower leaves the printer at 100 % | | |
| 28 | `/status` shows both printers live; 401 without the token | | |
| 29 | Thingiverse page → named file printed, license on the job | | |
| 30 | Printables page → named file printed, license on the job | | |
| 31 | Profile from Klipper: block, check, a cube printed | | |
| 32 | Imported Orca preset bundle sliced with its settings | | |
| 33 | `profiles diff` after an Orca upgrade | | |
| 34 | `check --sites`: Thingiverse token and Printables query | | |
| 35 | A 4-plate project as one batch across both printers | | |
| 36 | `/stage`: upload, slice, queue, offer on the phone | | |
| 37 | kWh per print within a few percent of the plug's counter | | |
