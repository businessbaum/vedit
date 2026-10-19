"""Every ffmpeg/ffprobe invocation in this repo goes through here.

Two hard rules encoded in this module (CONTRACT section 0.5 / 0.7):

1. argv lists only. Never a shell string, never Git Bash. Git Bash rewrites a
   filter argument containing ``C:`` into ``C\\;C:\\Program Files\\Git\\...``
   before ffmpeg ever sees it. That was reproduced live, not theorised.
2. Filtergraphs always go to a file, passed as ``-/filter_complex fc.txt``.
   Unconditionally: a 150-segment graph is ~19k chars and ~260 segments
   overflows the Windows command line with an unlaunchable process rather than
   a helpful error.
"""

from __future__ import annotations

import functools
import hashlib
import os
import shutil
import subprocess
from typing import Any


class VeditError(Exception):
    """Base for every error this package raises."""


class FFmpegError(VeditError):
    """A tool exited non-zero. Carries the stderr tail, which is the useful part."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr or ""
        tail = self.stderr.strip().splitlines()[-12:]
        super().__init__(
            f"{os.path.basename(args[0]) if args else '?'} exited {returncode}\n"
            + "  cmd: "
            + " ".join(args)
            + ("\n  " + "\n  ".join(tail) if tail else "")
        )


# --- tool resolution ----------------------------------------------------------
#
# Resolved once at import. Falls back to the bare name rather than raising, so
# importing the package on a machine without ffmpeg still works (tests that do
# not shell out keep running); the failure surfaces at the first run() instead.

_WINGET_BIN = os.path.join(
    os.environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"),
    "Microsoft",
    "WinGet",
    "Packages",
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
    "ffmpeg-8.1.1-full_build",
    "bin",
)


def _resolve_tool(name: str) -> str:
    env = os.environ.get("VEDIT_" + name.upper())
    if env and os.path.isfile(env):
        return env
    found = shutil.which(name)
    if found:
        return found
    fallback = os.path.join(_WINGET_BIN, name + ".exe")
    if os.path.isfile(fallback):
        return fallback
    return name


FFMPEG: str = _resolve_tool("ffmpeg")
FFPROBE: str = _resolve_tool("ffprobe")

_TOOLS = {"ffmpeg": FFMPEG, "ffmpeg.exe": FFMPEG, "ffprobe": FFPROBE, "ffprobe.exe": FFPROBE}


def run(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run an argv list. ``args[0]`` may be the bare name ``ffmpeg``/``ffprobe``.

    stdin is /dev/null unconditionally: ffmpeg reads stdin for its interactive
    keys and will block forever inside a non-interactive agent session.
    """
    if not args:
        raise ValueError("empty argv")
    argv = list(args)
    argv[0] = _TOOLS.get(os.path.basename(argv[0]).lower(), argv[0])
    proc = subprocess.run(
        argv,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        shell=False,  # explicit: see module docstring
        timeout=timeout,
        cwd=cwd,
        check=False,
    )
    if check and proc.returncode != 0:
        raise FFmpegError(argv, proc.returncode, proc.stderr if capture else "")
    return proc


def ffprobe_json(args: list[str]) -> dict[str, Any]:
    """Run ffprobe with the given arguments and parse its JSON output.

    ``args`` are the arguments AFTER the executable. ``-v error`` and
    ``-print_format json`` are added when absent so callers cannot accidentally
    get a banner mixed into the JSON.
    """
    import json

    argv = [FFPROBE]
    if "-v" not in args and "-loglevel" not in args:
        argv += ["-v", "error"]
    if "-of" not in args and "-print_format" not in args:
        argv += ["-print_format", "json"]
    argv += list(args)
    proc = run(argv)
    out = proc.stdout.strip()
    if not out:
        return {}
    return json.loads(out)


@functools.lru_cache(maxsize=1)
def tool_versions() -> dict[str, str]:
    """Version strings for cache keys.

    An encoder upgrade changes the bytes it produces, so it MUST be part of every
    render cache key or a re-encode silently reuses the old build's output.
    """
    versions: dict[str, str] = {}
    for name, exe in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        try:
            proc = run([exe, "-version"], check=False)
            first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
            # "ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright (c) ..."
            parts = first.split()
            versions[name] = parts[2] if len(parts) > 2 else "unknown"
        except (OSError, FFmpegError, IndexError):
            versions[name] = "unknown"
    return versions


# --- filtergraph files --------------------------------------------------------


def write_filtergraph(graph: str, work_dir: str) -> str:
    """Write a filtergraph to a file and return its path.

    Named by content hash rather than a fixed ``fc.txt`` so two renders sharing a
    work dir cannot clobber each other's graph mid-flight, and so re-running with
    an unchanged graph reuses the same file.

    UTF-8 without BOM: a BOM lands in the first filter name and ffmpeg reports a
    filter it has never heard of.

    Newlines inside the graph are safe -- verified: a one-line graph, one broken
    after a ``;``, and one with a trailing newline all produced the same 30
    frames -- so a generated graph may be written readably.
    """
    os.makedirs(work_dir, exist_ok=True)
    digest = hashlib.sha256(graph.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(work_dir, f"fc_{digest}.txt")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(graph)
    os.replace(tmp, path)
    return path


def filter_complex_args(graph_path: str) -> list[str]:
    """The two argv items that feed a filtergraph file to ffmpeg.

    ``-/filter_complex`` is ffmpeg's "read this option's value from a file" form.
    """
    return ["-/filter_complex", graph_path]


# --- path escaping inside filter arguments ------------------------------------
#
# A filter argument string is parsed TWICE: once by the filtergraph parser
# (quotes, backslashes, and the chain separators , ; [ ]) and again by
# av_opt_set_from_string (which splits on ':' and '='). A Windows drive colon has
# to survive both. Measured on ffmpeg 8.1.1 against a path containing a space:
#
#   subtitles=C:/x.ass          fail  Unable to parse "original_size" option value
#   subtitles=C\:/x.ass         fail  same -- one backslash is eaten by the first pass
#   subtitles=C\\:/x.ass        PASS
#   subtitles='C\:/x.ass'       PASS
#   subtitles='C\\:/x.ass'      fail  quotes already protect it; doubling over-escapes
#   ...:fontsdir=C\:/fonts      fail  No option name near '/Users/...'
#   ...:fontsdir=C\\:/fonts     PASS
#   ...:fontsdir='C\:/fonts'    PASS
#
# So the level of nesting is NOT what decides: quoting is. And with a path that
# also contains a comma, semicolon or bracket -- all legal in Windows filenames
# -- only the quoted form survives (measured: "No such filter: 'b.ass'" for
# a,b.ass in the unquoted doubled form, even with the comma escaped as well).


def escape_filter_path(p: str) -> str:
    r"""A Windows path as a complete, ready-to-paste filter option value.

    Returns the QUOTED single-escaped form INCLUDING its quotes::

        f"subtitles={escape_filter_path(ass)}:fontsdir={escape_filter_path(d)}"
        -> subtitles='C\:/Users/me/x.ass':fontsdir='C\:/Users/me/fonts'

    Do not add quotes of your own. This is the only spelling measured to work in
    every position and to survive spaces, commas, semicolons, brackets and
    umlauts, which is why it is the default rather than the bare form.
    """
    s = p.replace("\\", "/")
    s = s.replace("'", r"'\''")  # close, escaped literal quote, reopen
    s = s.replace(":", r"\:")
    return "'" + s + "'"


def escape_filter_path_nested(p: str) -> str:
    r"""The UNQUOTED doubled form: ``C\\:/Users/me/fonts``.

    This is the spelling the caption research names for ``fontsdir=``. It is
    verified working, but only for paths without ``, ; [ ]`` -- those are legal
    in Windows filenames and break the filtergraph parser before ffmpeg ever
    reaches the option. Prefer escape_filter_path(); use this only where a
    consumer genuinely cannot accept a quoted value.

    Failure it prevents: ``fontsdir=C\:/Users/...`` (single backslash, unquoted)
    dies with ``No option name near '/Users/...'`` -- the graph parser eats the
    one backslash and the option parser then splits on the bare colon.
    """
    s = p.replace("\\", "/")
    s = s.replace(":", r"\\:")
    return s
