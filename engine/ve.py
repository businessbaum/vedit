"""Entry point: ``python engine/ve.py <cmd>``.

Deliberately thin. Everything it does that the CLI module cannot:

1. Puts ``engine/`` on ``sys.path`` so ``vedit`` imports whether this is run as
   ``python engine/ve.py``, from another directory, or through tools/ve.ps1.
2. Forces UTF-8 on stdout/stderr. The Windows console defaults to cp1252 and a
   German transcript prints ``UnicodeEncodeError`` instead of ``Größe`` -- a
   crash caused entirely by the output encoding, in a pipeline whose whole point
   is German.
3. Refuses to run on a Python older than 3.11. The pipeline uses ``X | None``
   annotations at runtime and stdlib behaviour that is not there before that.
"""

from __future__ import annotations

import os
import sys

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass  # a redirected pipe that refuses reconfiguration is not fatal


def main() -> int:
    if sys.version_info < (3, 11):
        print(
            f"ve: needs Python 3.11 or newer, this is {sys.version.split()[0]} at {sys.executable}",
            file=sys.stderr,
        )
        return 1
    _force_utf8()
    from vedit.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
