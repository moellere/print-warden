"""`print-warden` runs the server; `print-warden check [config] [--sites]` checks a config without starting it;
`print-warden profiles ...` finds, imports or generates printer profiles."""

from __future__ import annotations

import sys


def main() -> None:
    if sys.argv[1:2] == ["check"]:
        from . import check

        sys.exit(check.main(sys.argv[2:]))
    if sys.argv[1:2] == ["profiles"]:
        from . import profiles

        sys.exit(profiles.main(sys.argv[2:]))
    from . import server

    server.main()
