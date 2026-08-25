"""PyInstaller entry-point shim for the `gateway-cli` console script.

Mirrors [tool.poetry.scripts] gateway-cli = "cli.main:main".
"""

import multiprocessing
import sys

# Korean Windows consoles default to cp949, which cannot encode characters like
# the em-dash (—) used in --help text; Click then dies with UnicodeEncodeError.
# Force UTF-8 on the standard streams so output never crashes on such consoles.
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
