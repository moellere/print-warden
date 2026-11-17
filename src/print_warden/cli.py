"""`print-warden` runs the server; `print-warden check [config] [--sites]` checks a config without starting it;
`print-warden profiles ...` finds, imports or generates printer profiles; `print-warden validate ...` runs the
live-validation runbook against the running warden; `print-warden demo` runs it all against simulated printers."""

from __future__ import annotations

import sys


def main() -> None:
    if sys.argv[1:2] == ["check"]:
        from . import check

        sys.exit(check.main(sys.argv[2:]))
    if sys.argv[1:2] == ["profiles"]:
        from . import profiles

        sys.exit(profiles.main(sys.argv[2:]))
    if sys.argv[1:2] == ["demo"]:
        from . import demo

        sys.exit(demo.main(sys.argv[2:]))
    if sys.argv[1:2] == ["validate"]:
        from . import validate

        sys.exit(validate.main(sys.argv[2:]))
    from . import server

    server.main()
