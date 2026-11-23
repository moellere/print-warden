# Live validation

The test suite runs against fakes: a fake Moonraker, a fake Home Assistant, a fake broker. This runbook is where the
warden meets the real printers, cameras, phone and Orca. Run it after a deploy that changes behaviour, and sign off
each row per printer (date and initials, or an issue link when it fails).

Before anything else, `print-warden check` (in the container: `docker compose exec print-warden print-warden check`)
must report `0 failed`. It confirms the profiles resolve and that Moonraker, the cameras, Home Assistant, Spoolman
and the broker answer, without printing or publishing anything.

Use a small, fast model (a 20 mm calibration cube in PLA at `draft`) unless a row says otherwise.

`print-warden validate` (in the container: `docker compose exec print-warden print-warden validate …`) runs this
runbook against the running warden. It reaches the warden over `/mcp` with `PRINT_WARDEN_TOKEN`, so it starts
nothing: a setup that offers a job still waits for the tap.
- `validate` lists every row with its kind and sign-off per printer:
  - **auto** rows check themselves;
  - **assisted** rows check what follows from your step on the phone or printer;
  - **manual** rows are judged by eye or with another tool.
- `validate <row> --printer P [--setup] [--by EM]` prints your step, sets up what it can with `--setup` (after a
  y/N), then checks the outcome against the warden's own state, polling for the row's time (`--wait` minutes
  overrides it). A run whose checks pass is signed with `--by`. Each run is kept in `<storage>/validation.json`.
- What the setups do:
  - row 1 slices and stages a test cube;
  - rows 4 and 5 send its Start offer;
  - row 23 splits six cubes;
  - row 35 queues a batch of four cubes (or every plate of `--model`);
  - row 38 sends `PAUSE` to the printer's running warden print;
  - row 39 queues `fault_test`;
  - row 41 takes the printer-health snapshot.
- `validate sign <row> --printer P --by EM` signs a manual row; `--issue <url>` records a failing one;
  `--na "<why>"` records that the row does not apply to that printer. `validate sign all --printer P --na "<why>"`
  marks every row not already settled on a printer you are leaving out (n/a counts as settled).
- `validate report` prints the table below, filled in, for pasting here.
- `validate ready` prints the 1.0 checklist: per printer, each row still open with its kind, whether CI already
  proves the warden's side or one run covers every printer, and the command that settles it. It exits 0 only when
  every row is signed, has an issue, or is n/a. A session ends with the same count on the phone and in its
  sign-off file.

**A whole printer in one go:** `print-warden validate session --printer garagex4 --by EM` walks every row in
order and asks each step on the phone.
- A setup runs only after you tap Go.
- A checkable row is checked until it passes, with Skip on the phone meanwhile. If it doesn't pass, you get Retry,
  Fail (with a typed note) or Skip.
- A row judged by eye is a Pass / Fail / Skip tap.
- Signed rows and rows with an issue are passed over, so stopping and starting the session again resumes it.
- `--rows 1-13,39` limits it, and `--terminal` asks at the terminal instead of on the phone.
- Rows marked n/a are passed over like signed ones.
- At the end it writes `<storage>/signoff-<printer>-<date>.md`: the filled-in table and each failure with its note
  and failing checks, ready to paste here or file as issues.

**Record while you validate:** in a second shell, `print-warden record --printer garagex4 --out
tests/data/recordings/garagex4-fault.jsonl` before row 39. Do the same for row 13 (`-cancel`) and row 25
(`-shutdown`). Each recording keeps one print as the printer reported it, with the file names replaced. Committed
under `tests/data/recordings/`, it is replayed in CI on every change (`tests/test_record.py`).

Rehearse first with `print-warden demo`, which runs the warden against simulated printers:
1. Run it with `PRINT_WARDEN_TOKEN=demo print-warden validate --config print-warden-demo/printers.yaml <row>
   --printer demo-a --setup`.
2. Tap on the simulated phone at `http://127.0.0.1:8711/sim/phone`.
3. Use each printer's console there for `PAUSE`, `M112` and `FIRMWARE_RESTART`.

The rows CI already runs against the demo are 0, 1, 3, 4, 10–13, 23, 25, 28, 35, 38, 39 and 41. They prove the
warden's side of each row, so on the printers only the hardware's side is left.

## 1. Slice and stage

1. `submit_job(printer, model="cube.stl", material="PLA", quality="draft", offer=False)`.
   Expect: estimates, a preview URL that opens, no blockers, the job `staged`.
2. Upload a `.3mf` exported from Orca, Bambu Studio and PrusaSlicer (one each, at least one with two plates);
   `submit_job(..., plate=2)`. Expect: the plate's objects only, a warning naming the plates, no project gcode in
   the job's gcode (its start gcode is the printer profile's). One with a negative part (a hole cut by a volume):
   the hole is in the print.
3. A model larger than the bed. Expect: refused before slicing, saying what does not fit (the footprint against the
   bed, or the height).

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
11. Remove the part, tap **Bed cleared** (or press it on `/status`: same result). Expect: a labelled check recorded; the next queued job is offered.
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
38. With `policy.paused_cancel_minutes: 2`, `PAUSE` a print and leave it. Expect: the pause alert names the time;
    two minutes later the print is cancelled, both heater targets read 0 and a warning alert says why. Then start a
    print, send a gcode that errors mid-print (or cancel it from Fluidd on a printer whose `CANCEL_PRINT` keeps the
    heaters on): within two minutes the targets read 0 and an alert says the warden switched them off.

39. `fault_test(printer, "<you>")` (or `validate 39 --setup`) queues a cube whose gcode errors at layer 2; tap
    Start. Expect: the phone prompt names the cause ("move out of range") with what Klipper said and the last
    command; within two minutes the heater targets read 0; **Reprint** stages the same gcode as a new job and offers
    it for a tap (Skip it). `printer_stats` does not count the fault test's failure.

## 5. Alerts

16. Inside `quiet_hours`, or with `quiet_entity` in a quiet state: a warning (unplug a fan's tach or set a low
    `temp_limits` value) is held; a critical alert still arrives.
17. Toggle `quiet_entity` off. Expect: the next warning arrives.
40. With `presence.entity` set and `presence.away_offer_max_hours: 1`, leave home (or set the person `not_home` in
    Developer tools) with a 2 h job queued and `auto_start` on. Expect: no offer and no start (`offer_next` says
    "nobody is home"); a warning inside quiet hours still arrives. Come back: one "while you were out" alert, then
    the offer.
41. `printer_health(printer)` (takes the snapshot), then change a harmless setting in `printer.cfg` (e.g.
    `max_accel`) and restart Klipper. Expect: `printer_health` lists `[printer] max_accel: a -> b`, an hourly alert
    says so once, `/healthz` shows `health: ["config_changed"]`; `accept=True` clears it.

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
    For a whole new printer, `print-warden add-printer <name> --moonraker http://<printer>:7125 --write
    data/printers.yaml` does this, and the entry, in one step (README, "Adding a printer").
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

## 15. Spool inventory

42. On `/spools`, register a spool from a new box (vendor, material, colour, empty-spool weight from the vendor or
    an empty spool on the scale). Expect: it appears in Spoolman with its filament, and the page gives its code; write
    it on the spool. Swap it onto a printer with the outgoing spool on the scale. Expect: the old spool's remaining
    weight is corrected, the new one is active, and a material that does not suit the queue is named.
43. With `upkeep.weigh_every_days: 1`, wait for the weigh-in prompt, type the scale reading into the reply. Expect:
    Spoolman's remaining weight follows the scale; a filament with a deliberately wrong density in Spoolman shows a
    drift over 5 % after ~100 g of printing.

44. With `upkeep.todo_entity` set and a material under `reorder_below_g`: within the hour "PLA filament" is on the
    HA list once; register a new spool and it comes off. With a `spool_scale_entity`: lift the spool off and back
    while the printer is idle; the next hourly pass records a weigh-in.

## 16. Weekly digest

45. Set `digest:` to today and a time an hour or two ahead, restart, and wait past it. Expect: one "Week in prints"
    on the phone with the week's prints, failures and what is due (the same as `weekly_digest`), and no second one
    an hour later. With away mode, set the person away before the time: nothing arrives until you are back.

## 17. The week's plan

46. With `policy.plan_offers: true`, a `finish_window` and quiet hours set, queue a job in the evening long enough
    to finish outside the window. Expect: `plan_week` and `/status` put it at the first morning slot, no offer
    arrives overnight, and the offer arrives at that slot.

## 18. Inbox

47. With `PRINT_WARDEN_OPERATOR_PASSWORD` set, offer a job and swipe the notification away. Open `/inbox` on the
    phone or a laptop: it sends you to sign in, then shows the Start offer. Press Start. Expect: the print starts,
    its log says "the web inbox", and the prompt is gone from both the inbox and the phone. Then sign out and open
    `/inbox?token=<PRINT_WARDEN_TOKEN>`: it sends you to sign in rather than showing the buttons.

## Sign-off

wyolax1 is at another location and is **not applicable for 1.0** (owner's decision, 2026-11-23): 1.0 is cut on
garagex4's sign-off. Its column is filled in when it is next reachable.

| # | Row | garagex4 | wyolax1 |
|---|---|---|---|
| 0 | `print-warden check` reports 0 failed | | n/a |
| 1 | Slice and stage an STL | | n/a |
| 2 | 3MF from Orca / Bambu Studio / PrusaSlicer, plate 2 | | n/a |
| 3 | Oversized model refused before slicing | | n/a |
| 4 | Start by tap | | n/a |
| 5 | Expired Start tap refused | | n/a |
| 6 | First-layer prompt | | n/a |
| 7 | Completion: alert, Spoolman, timelapse, park | | n/a |
| 8 | Outcome prompt labels the job; "failed before" warning | | n/a |
| 9 | HA entities follow the print | | n/a |
| 10 | Occupied bed blocks the next start | | n/a |
| 11 | Bed cleared → labelled check → next job offered | | n/a |
| 12 | Bed check sees a part | | n/a |
| 13 | Cancel by tap; stale Cancel ignored | | n/a |
| 14 | Obico pause → Resume | | n/a |
| 15 | Restart mid-print, settled from history | | n/a |
| 16 | Quiet hours / quiet entity hold warnings, not criticals | | n/a |
| 17 | Quiet entity off → warnings resume | | n/a |
| 18 | MQTT device, rename survives HA restart, unavailable when stopped | | n/a |
| 19 | homelab-helper `printer-offline` opens and resolves | | n/a |
| 20 | Maintenance due → Done resets it and the sensor | | n/a |
| 21 | Reorder alert once a day | | n/a |
| 22 | Drying reminder → Dried resets it | | n/a |
| 23 | submit_split shares copies; each share tap-started | | n/a |
| 24 | Cheap-power hold, offered when the price drops | | n/a |
| 25 | Shutdown cause in `/healthz` and as a homelab-helper finding | | n/a |
| 26 | Temperature tower → section saved to the spool and used | | n/a |
| 27 | Flow tower leaves the printer at 100 % | | n/a |
| 28 | `/status` shows both printers live; 401 without the token | | n/a |
| 29 | Thingiverse page → named file printed, license on the job | | n/a |
| 30 | Printables page → named file printed, license on the job | | n/a |
| 31 | Profile from Klipper: block, check, a cube printed | | n/a |
| 32 | Imported Orca preset bundle sliced with its settings | | n/a |
| 33 | `profiles diff` after an Orca upgrade | | n/a |
| 34 | `check --sites`: Thingiverse token and Printables query | | n/a |
| 35 | A 4-plate project as one batch across both printers | | n/a |
| 36 | `/stage`: upload, slice, queue, offer on the phone | | n/a |
| 37 | kWh per print within a few percent of the plug's counter | | n/a |
| 38 | A paused print cancels itself, heaters off; a failed print is cooled | | n/a |
| 39 | An errored print names its cause; Reprint queues it | | n/a |
| 40 | Away mode: no offer or auto-start away; summary and offer on return | | n/a |
| 41 | Printer health: a printer.cfg change is a finding until accepted | | n/a |
| 42 | Spool registered, labelled, swapped on with the old one weighed | | n/a |
| 43 | Weigh-in reply corrects Spoolman; a wrong density shows as drift | | n/a |
| 44 | Reorder on the HA to-do list and off again; the spool scale weighs | | n/a |
| 45 | Weekly digest arrives once; held while away | | n/a |
| 46 | The plan holds an evening job and offers it at its morning slot | | n/a |
| 47 | A swiped-away Start answered from /inbox; the token alone is refused | | n/a |
