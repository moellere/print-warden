"""`print-warden` runs the server; `print-warden check [config]` checks a config without starting it."""

from __future__ import annotations

import sys


def main() -> None:
    if sys.argv[1:2] == ["check"]:
        from . import check

        sys.exit(check.main(sys.argv[2:]))
    from . import server

    server.main()
