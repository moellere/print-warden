"""`print-warden` runs the server; the verbs in `VERBS` do everything else (see docs/stability.md)."""

from __future__ import annotations

import sys

VERBS = {
    "check": "check a config without starting the warden ([config] [--sites])",
    "profiles": "find, import or generate printer profiles",
    "validate": "run the live-validation runbook against the running warden",
    "demo": "run the warden against simulated printers",
    "backup": "write the warden's state to one archive",
    "restore": "put a backup's state back",
}


def main() -> None:
    verb = sys.argv[1] if len(sys.argv) > 1 else ""
    rest = sys.argv[2:]
    if verb == "check":
        from . import check

        sys.exit(check.main(rest))
    if verb == "profiles":
        from . import profiles

        sys.exit(profiles.main(rest))
    if verb == "validate":
        from . import validate

        sys.exit(validate.main(rest))
    if verb == "demo":
        from . import demo

        sys.exit(demo.main(rest))
    if verb in ("backup", "restore"):
        from . import backup

        sys.exit(backup.main(verb, rest))
    if verb in ("-h", "--help", "help"):
        print("print-warden: run the warden. Or print-warden <verb>:")
        for v, what in VERBS.items():
            print(f"  {v:<9} {what}")
        sys.exit(0)
    from . import server

    server.main()
