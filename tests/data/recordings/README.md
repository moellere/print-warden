Recordings `print-warden record` made, which the simulator replays in `tests/test_record.py`.

- `sim-fault.jsonl` and `sim-shutdown.jsonl` were recorded from `print-warden demo`, so they hold the simulator's own
  behaviour and prove only the record → replay path. They are not evidence of what Klipper does.
- Recordings from the real printers go next to them as `<printer>-<what>.jsonl` (for example `garagex4-fault.jsonl`),
  made with `print-warden record --printer garagex4 --out tests/data/recordings/garagex4-fault.jsonl` during the
  runbook's rows 39 (fault test), 13 (cancel) and 25 (M112). `test_record.py` replays every file here.
