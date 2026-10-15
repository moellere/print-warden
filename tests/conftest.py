"""The server module loads its config at import, so point it at a throwaway one before any test imports it."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_ROOT = Path(tempfile.mkdtemp(prefix="print-warden-test-"))
(_ROOT / "printers.yaml").write_text(
    f"""
printers:
  testp:
    location: lab
    moonraker: http://127.0.0.1:9
    cameras: {{}}
    slicer:
      machine: M
      processes: {{standard: P}}
      filaments: {{PLA: F}}
home_assistant:
  url: http://127.0.0.1:9
  webhook_id: test
storage:
  dir: {_ROOT / "data"}
  keep_days: 14
policy:
  approval_ttl_minutes: 15
"""
)
os.environ["PRINT_WARDEN_CONFIG"] = str(_ROOT / "printers.yaml")
os.environ["PRINT_WARDEN_TOKEN"] = "test-token"
os.environ["OBICO_WEBHOOK_SECRET"] = "obico-secret"
os.environ["HA_ACTION_SECRET"] = "ha-secret"
