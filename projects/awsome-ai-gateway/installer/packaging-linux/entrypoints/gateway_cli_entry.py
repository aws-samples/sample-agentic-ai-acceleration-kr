"""PyInstaller entry-point shim for the `gateway-cli` console script.

Mirrors [tool.poetry.scripts] gateway-cli = "cli.main:main".
"""

import multiprocessing
import sys

# Most Linux/WSL terminals are UTF-8 already, but a bare locale (LANG=C / POSIX)
# leaves stdout on ascii, which then dies with UnicodeEncodeError on the em-dash
# (—) used in --help text. Force UTF-8 on the standard streams so output never
# crashes regardless of the ambient locale.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from cli.main import main

if __name__ == "__main__":
    # Required for frozen executables in case anything spawns worker processes.
    multiprocessing.freeze_support()
    sys.exit(main())
