"""The HTML graphics lane: template -> PNG sequence -> alpha movie -> composite.

CONTRACT invariant 6 is the whole design: **every graphic is a pure function of
``?t=<seconds>``**. No CSS animations, no requestAnimationFrame, no timeline
library. The page reads t out of ``location.search``, computes one static
layout, and paints it.

That single rule is what makes this lane work at all. ``chrome --screenshot``
fires "when the page looks loaded" -- there is no virtual clock, no
``Emulation.setVirtualTimePolicy``, no ``beginFrame``. Any self-animating page
sampled that way returns a different image every run, which destroys
content-addressed caching (same key, different pixels) and puts judder in the
output. A pure function of t is deterministic, so every frame is cacheable and a
re-render of an unchanged graphic costs nothing.

Two silent failures are guarded here rather than discovered later:

- **A Git-Bash-style file:// path fails SILENTLY.** Chrome renders its blank
  dark error page, writes a plausible PNG and exits 0, so three different
  templates produce three identical images and nothing complains.
  ``chrome_screenshot()`` refuses non-native paths and compares every output
  against the measured blank-page hash for that window size.
- **Alpha is easy to lose in the carrier.** VP9-in-WebM claims yuva420p and
  writes an opaque file. qtrle/argb round-trips exactly and is ~13x smaller
  than ProRes 4444 on flat graphics, so it is the carrier here, and
  ``probe_alpha()`` reads one raw rgba pixel back to prove it (QC check d).
"""

from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Iterable, Mapping, Sequence

from .ffmpeg import FFMPEG, VeditError, filter_complex_args, run, write_filtergraph
from .keys import cache_key, repo_root
from .timebase import Timebase


class GfxError(VeditError):
    """The graphics lane could not produce a trustworthy frame."""


# --- chrome -------------------------------------------------------------------

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Exactly the flag set from CONTRACT section "GRAPHICS". --default-background-color
# is the one that matters most: 00000000 is what produces genuine alpha rather
# than a white page, and it is 8 hex digits (ARGB), not 6.
_CHROME_FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--hide-scrollbars",
    "--default-background-color=00000000",
)

_SCREENSHOT_TIMEOUT_S = 120.0

_thread_local = threading.local()


def chrome_exe() -> str:
    env = os.environ.get("VEDIT_CHROME")
    if env and os.path.isfile(env):
        return env
    for c in _CHROME_CANDIDATES:
        if os.path.isfile(c):
            return c
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found
    raise GfxError("Chrome not found; set VEDIT_CHROME to chrome.exe")


_VERSION_DIR_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")


@functools.lru_cache(maxsize=1)
def chrome_version() -> str:
    """Version string, part of every frame cache key.

    A browser upgrade changes rasterisation, so a cached PNG from the old build
    is not the same picture. Without this in the key the cache lies after every
    Chrome auto-update.

    Read from the version directory beside chrome.exe, NOT from
    ``chrome --version``: on Windows that flag opens a real browser window and
    never returns, so a naive version probe hangs the whole render.
    """
    try:
        base = os.path.dirname(chrome_exe())
        dirs = [d for d in os.listdir(base) if _VERSION_DIR_RE.fullmatch(d)]
        if dirs:
            return sorted(dirs, key=lambda s: tuple(int(x) for x in s.split(".")))[-1]
    except (OSError, GfxError):
        pass
    return "unknown"


_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
# One leading slash and no second one: a POSIX absolute path. Two is a UNC
# share, which IS native on Windows and must stay allowed.
_POSIX_ABS_RE = re.compile(r"^[\\/](?![\\/])")


def assert_native_windows_path(path: str) -> str:
    r"""Reject a POSIX-shaped path before it becomes a silent blank render.

    ``/c/Users/...`` and ``/cygdrive/c/...`` are what Git Bash hands you. Chrome
    treats them as a missing file, renders its blank error page, writes the PNG
    and exits 0 -- so the whole sequence comes out identical and plausible until
    somebody watches it.

    The check runs on the RAW string, before any abspath(). ``os.path.abspath``
    turns ``/c/Users/x.html`` into ``C:\c\Users\x.html``, which passes a
    drive-letter test while pointing at nothing -- so validating the normalised
    form is exactly the check that does not work.
    """
    if _POSIX_ABS_RE.match(path) or path.lower().startswith(("/cygdrive/", "/mnt/")):
        raise GfxError(
            f"not a native Windows path: {path!r}. Chrome renders a blank error page for "
            "POSIX-style paths, writes the PNG anyway and exits 0."
        )
    absolute = os.path.abspath(path)
    if not (_WIN_ABS_RE.match(absolute) or absolute.startswith("\\\\")):
        raise GfxError(f"not a native Windows path: {path!r} -> {absolute!r}")
    return absolute


def file_url(path: str, params: Mapping[str, Any] | None = None) -> str:
    """A ``file://`` URL for a native Windows path, plus an optional query.

    The file must exist. Chrome's response to a missing file is a blank page and
    exit 0, so refusing here converts an invisible bug into a stack trace.
    Values are percent-encoded, so German props survive intact.
    """
    ap = assert_native_windows_path(path)
    if not os.path.exists(ap):
        raise GfxError(f"no such file to render: {ap}")
    url = "file:///" + urllib.parse.quote(ap.replace("\\", "/"), safe="/:")
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return url


def _profile_dir() -> str:
    """One Chrome profile per worker thread.

    Concurrent Chrome processes sharing a --user-data-dir fight over the profile
    lock; one of them silently becomes a no-op relauncher and writes nothing.
    """
    d = getattr(_thread_local, "profile", None)
    if d is None:
        d = tempfile.mkdtemp(prefix="vedit_chrome_")
        _thread_local.profile = d
    return d


def _screenshot_raw(url: str, out_png: str, size: tuple[int, int], *, user_data_dir: str | None = None) -> None:
    w, h = int(size[0]), int(size[1])
    out = os.path.abspath(out_png)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        os.unlink(out)
    argv = [
        chrome_exe(),
        *_CHROME_FLAGS,
        f"--window-size={w},{h}",
        f"--screenshot={out}",
        f"--user-data-dir={user_data_dir or _profile_dir()}",
        url,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        shell=False,  # argv list only, per CONTRACT 0.5
        timeout=_SCREENSHOT_TIMEOUT_S,
        check=False,
    )
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        tail = "\n  ".join((proc.stderr or "").strip().splitlines()[-8:])
        raise GfxError(f"chrome wrote no screenshot for {url}\n  exit={proc.returncode}\n  {tail}")


_blank_lock = threading.Lock()
_blank_hashes: dict[tuple[int, int], str] = {}


def blank_hash(size: tuple[int, int]) -> str:
    """sha256 of Chrome's blank error page at this window size.

    Measured once per size rather than hardcoded: the error page is themed and
    versioned, so a literal constant would rot at the next Chrome update and the
    guard would quietly stop guarding.
    """
    key = (int(size[0]), int(size[1]))
    with _blank_lock:
        if key in _blank_hashes:
            return _blank_hashes[key]
    tmp = tempfile.mkdtemp(prefix="vedit_blank_")
    try:
        png = os.path.join(tmp, "blank.png")
        missing = os.path.join(repo_root(), "__vedit_no_such_template__.html")
        _screenshot_raw(file_url_unchecked(missing), png, key, user_data_dir=os.path.join(tmp, "prof"))
        digest = _sha256_file(png)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with _blank_lock:
        _blank_hashes[key] = digest
    return digest


def file_url_unchecked(path: str) -> str:
    """file:// URL that does not require the file to exist. Used for the probe."""
    return "file:///" + urllib.parse.quote(os.path.abspath(path).replace("\\", "/"), safe="/:")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def chrome_screenshot(url: str, out_png: str, size: tuple[int, int]) -> None:
    """Render one URL to one PNG at ``size``, with alpha, deterministically.

    Raises GfxError if the result is Chrome's blank error page -- the failure
    mode that otherwise produces an entire sequence of identical frames with
    exit 0 and plausible file sizes.
    """
    _screenshot_raw(url, out_png, size)
    if _sha256_file(out_png) == blank_hash(size):
        raise GfxError(
            f"chrome rendered its blank error page for {url} -- the URL did not resolve. "
            "Check that the template path is a native Windows path and the file exists."
        )


# --- template fingerprinting --------------------------------------------------

_LOCAL_REF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"':#?]+)["']""", re.I)
_CSS_URL_RE = re.compile(r"""url\(\s*["']?([^"')?#]+)["']?\s*\)""", re.I)


def template_fingerprint(template_path: str) -> str:
    """Hash of the template AND every local file it pulls in.

    A shared ``_base.css`` or a swapped font changes the pixels without changing
    the HTML, so hashing the HTML alone would serve stale frames from the cache
    for the rest of the project's life.
    """
    h = hashlib.sha256()
    seen: set[str] = set()
    queue = [os.path.abspath(template_path)]
    while queue:
        path = queue.pop(0)
        key = os.path.normcase(path)
        if key in seen or not os.path.isfile(path):
            continue
        seen.add(key)
        with open(path, "rb") as fh:
            data = fh.read()
        h.update(os.path.basename(path).encode("utf-8"))
        h.update(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # a font or an image: hashed, not scanned
        here = os.path.dirname(path)
        for ref in _LOCAL_REF_RE.findall(text) + _CSS_URL_RE.findall(text):
            if ref.startswith(("http:", "https:", "data:", "//")):
                continue
            queue.append(os.path.normpath(os.path.join(here, ref)))
    return h.hexdigest()


def resolve_template(template: str) -> str:
    """Accept an absolute path or a repo-relative one like ``presets/graphics/x.html``."""
    if os.path.isabs(template):
        path = template
    else:
        path = os.path.join(repo_root(), template)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise GfxError(f"template not found: {path}")
    return path


# --- frame rendering ----------------------------------------------------------


def frame_params(
    frame: int, duration_frames: int, tb: Timebase, size: tuple[int, int], props: Mapping[str, Any]
) -> dict[str, str]:
    """The query string a template sees. ``t`` is the only clock it may use."""
    return {
        "t": tb.secs(frame),
        "d": tb.secs(duration_frames),
        "f": str(frame),
        "n": str(duration_frames),
        "w": str(int(size[0])),
        "h": str(int(size[1])),
        "p": json.dumps(props, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    }


def render_html_frames(
    template: str,
    props: Mapping[str, Any],
    duration_frames: int,
    tb: Timebase,
    cache: Any,
    *,
    size: tuple[int, int] = (1920, 1080),
    workers: int = 4,
    progress: Any = None,
) -> list[str]:
    """Render ``duration_frames`` PNGs of a template, one per frame. Cached.

    Returns absolute PNG paths in frame order. Each frame is keyed on the
    template fingerprint, the props, the size, the frame index, the timebase and
    the Chrome version, so an unchanged graphic re-renders for free and a
    changed one cannot serve a stale frame.

    Parallel by default: a screenshot costs ~450 ms steady-state and the work is
    one subprocess per frame, so four workers is close to linear. Chrome's cold
    start (~8 s) is paid once per worker, not per frame.
    """
    path = resolve_template(template)
    fp = template_fingerprint(path)
    ver = chrome_version()
    props = dict(props or {})
    size = (int(size[0]), int(size[1]))

    plan: list[tuple[int, str, str]] = []  # (frame, key, final path)
    for f in range(int(duration_frames)):
        key = cache_key(
            {
                "renderer": "chrome",
                "renderer_version": ver,
                "kind": "html_frame",
                "template": os.path.basename(path),
                "fingerprint": fp,
                "props": props,
                "size": [size[0], size[1]],
                "frame": f,
                "duration_frames": int(duration_frames),
                "fps_num": tb.fps_num,
                "fps_den": tb.fps_den,
            }
        )
        plan.append((f, key, cache.path_for("html", key, ".png")))

    todo = [(f, key, dest) for f, key, dest in plan if not cache.has("html", key, ".png")]

    def shoot(job: tuple[int, str, str]) -> tuple[int, str, str, float]:
        """Screenshot only. The commit happens on the calling thread -- the
        cache index is a single sqlite connection and sqlite refuses to be used
        from a thread other than the one that opened it."""
        f, key, _dest = job
        started = time.perf_counter()
        tmp = cache.tmp_for("html", key, ".png")
        url = file_url(path, frame_params(f, int(duration_frames), tb, size, props))
        chrome_screenshot(url, tmp, size)
        return f, key, tmp, (time.perf_counter() - started) * 1000.0

    if todo:
        n = max(1, min(int(workers), len(todo)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n, thread_name_prefix="vedit_gfx") as pool:
            for done, (f, key, tmp, ms) in enumerate(pool.map(shoot, todo), 1):
                cache.commit(
                    tmp,
                    "html",
                    key,
                    ".png",
                    {
                        "template": os.path.basename(path),
                        "fingerprint": fp,
                        "frame": f,
                        "t": tb.secs(f),
                        "size": [size[0], size[1]],
                        "renderer": "chrome",
                        "renderer_version": ver,
                        "props": props,
                        "render_ms": round(ms, 1),
                    },
                )
                if progress:
                    progress(done, len(todo), f, ms)

    return [dest for _f, _k, dest in plan]


def stage_frames(paths: Sequence[str], out_dir: str, *, prefix: str = "f_") -> str:
    """Hard-link content-addressed PNGs into a numbered sequence for ffmpeg.

    The image2 demuxer wants ``f_%06d.png``; the cache stores by content hash.
    Hard links keep this free and keep the cache the only copy of the bytes.
    """
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.startswith(prefix) and old.endswith(".png"):
            os.unlink(os.path.join(out_dir, old))
    for i, src in enumerate(paths):
        dst = os.path.join(out_dir, f"{prefix}{i:06d}.png")
        try:
            os.link(src, dst)
        except OSError:
            # Different volume, or a filesystem without hard links.
            shutil.copyfile(src, dst)
    return out_dir


def frames_to_alpha_movie(png_dir: str, out_path: str, tb: Timebase, *, prefix: str = "f_") -> str:
    """PNG sequence -> QuickTime RLE with a real alpha channel. Returns out_path.

    qtrle/argb rather than ProRes 4444: both are lossless for this content and
    both round-trip a=0/128/255 exactly, but qtrle is ~13x smaller on flat
    graphics. VP9-in-WebM is not an option -- it advertises yuva420p, writes an
    opaque file, and never says so.
    """
    pattern = os.path.join(os.path.abspath(png_dir), f"{prefix}%06d.png")
    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run(
        [
            FFMPEG,
            "-y",
            "-framerate",
            f"{tb.fps_num}/{tb.fps_den}",
            "-start_number",
            "0",
            "-i",
            pattern,
            "-c:v",
            "qtrle",
            "-pix_fmt",
            "argb",
            # The PNG sequence is already CFR by construction; passthrough stops
            # ffmpeg from inventing duplicate or dropped frames at odd rates.
            "-fps_mode",
            "passthrough",
            out,
        ]
    )
    return out


def probe_alpha(video_path: str, x: int = 0, y: int = 0, *, frame: int = 0) -> tuple[int, int, int, int]:
    """Read one raw RGBA pixel out of a rendered graphic. QC check (d).

    ``a == 0`` at a known-transparent point is the only trustworthy proof that
    alpha survived: the encoder log claims alpha even when the file has none.
    """
    tmp = tempfile.mkdtemp(prefix="vedit_alpha_")
    try:
        raw = os.path.join(tmp, "px.raw")
        run(
            [
                FFMPEG,
                "-y",
                "-i",
                os.path.abspath(video_path),
                "-vf",
                f"select=eq(n\\,{int(frame)}),format=rgba,crop=1:1:{int(x)}:{int(y)}",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgba",
                raw,
            ]
        )
        with open(raw, "rb") as fh:
            data = fh.read(4)
        if len(data) != 4:
            raise GfxError(f"could not read an rgba pixel from {video_path}")
        return (data[0], data[1], data[2], data[3])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def frame_hashes(paths: Iterable[str]) -> list[str]:
    """Short content hashes, in order. Proves a t-driven template really moves."""
    return [_sha256_file(p)[:16] for p in paths]


# --- one overlay, end to end --------------------------------------------------


def render_overlay(
    template: str,
    props: Mapping[str, Any],
    duration_frames: int,
    tb: Timebase,
    cache: Any,
    *,
    size: tuple[int, int] = (1920, 1080),
    workers: int = 4,
    work_dir: str | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Template + props -> a cached alpha .mov. Returns the overlay render record.

    The record is what goes into ``project.json`` under ``overlays[].render``:
    ``{"cache_key": ..., "path": ..., "frames": ...}``.
    """
    path = resolve_template(template)
    key = cache_key(
        {
            "renderer": "chrome+qtrle",
            "renderer_version": chrome_version(),
            "kind": "overlay",
            "template": os.path.basename(path),
            "fingerprint": template_fingerprint(path),
            "props": dict(props or {}),
            "size": [int(size[0]), int(size[1])],
            "duration_frames": int(duration_frames),
            "fps_num": tb.fps_num,
            "fps_den": tb.fps_den,
        }
    )
    if cache.has("ovl", key, ".mov", expect_frames=int(duration_frames)):
        return {"cache_key": key, "path": cache.path_for("ovl", key, ".mov"), "frames": int(duration_frames)}

    started = time.perf_counter()
    pngs = render_html_frames(
        template, props, duration_frames, tb, cache, size=size, workers=workers, progress=progress
    )
    staged = work_dir or tempfile.mkdtemp(prefix="vedit_seq_")
    try:
        stage_frames(pngs, staged)
        tmp = cache.tmp_for("ovl", key, ".mov")
        frames_to_alpha_movie(staged, tmp, tb)
        final = cache.commit(
            tmp,
            "ovl",
            key,
            ".mov",
            {
                "template": os.path.basename(path),
                "props": dict(props or {}),
                "size": [int(size[0]), int(size[1])],
                "frames": int(duration_frames),
                "codec": "qtrle/argb",
                "render_ms": round((time.perf_counter() - started) * 1000.0, 1),
            },
        )
    finally:
        if work_dir is None:
            shutil.rmtree(staged, ignore_errors=True)
    return {"cache_key": key, "path": final, "frames": int(duration_frames)}


# --- compositing --------------------------------------------------------------


def composite_overlays(
    base_video: str,
    overlays: Sequence[Mapping[str, Any]],
    out_path: str,
    tb: Timebase,
    *,
    work_dir: str | None = None,
    v_codec: str = "libx264",
    extra_video: Sequence[str] | None = None,
    map_audio: bool = True,
) -> str:
    """Burn alpha overlays onto a video at their timeline positions.

    Each overlay is ``{"path": <mov>, "tl_in_frames": int, "x": int|expr,
    "y": int|expr, "layer": int}``; ``x``/``y`` default to 0 because the
    templates render at full frame size and place themselves internally.

    Layer order is explicit and stable: sorted by ``layer`` then by
    ``tl_in_frames``, so two graphics on the same frame always stack the same
    way regardless of dict ordering.

    The graph goes to a FILE unconditionally (CONTRACT 0.7) -- a dozen overlays
    is already long enough that inlining it is a bet on the Windows command line.
    """
    base = os.path.abspath(base_video)
    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    items = sorted(overlays, key=lambda o: (int(o.get("layer", 0)), int(o.get("tl_in_frames", 0))))

    args: list[str] = [FFMPEG, "-y", "-i", base]
    parts: list[str] = []
    label = "0:v"
    for i, ov in enumerate(items, start=1):
        args += ["-i", os.path.abspath(str(ov["path"]))]
        delay = tb.secs(int(ov.get("tl_in_frames", 0)))
        x = ov.get("x", 0)
        y = ov.get("y", 0)
        # format=rgba before setpts: the overlay filter needs an alpha-carrying
        # format on the top input, and setpts is what positions the graphic on
        # the timeline. eof_action=pass keeps the base running after it ends.
        parts.append(f"[{i}:v]format=rgba,setpts=PTS-STARTPTS+{delay}/TB[g{i}]")
        nxt = f"v{i}"
        parts.append(f"[{label}][g{i}]overlay=x={x}:y={y}:eof_action=pass:format=auto[{nxt}]")
        label = nxt
    if not items:
        parts.append("[0:v]null[vout]")
        label = "vout"

    graph = ";\n".join(parts)
    gp = write_filtergraph(graph, work_dir or os.path.dirname(out) or ".")
    args += filter_complex_args(gp)
    args += ["-map", f"[{label}]"]
    if map_audio:
        args += ["-map", "0:a?", "-c:a", "copy"]  # '?' = optional, silent-audio bases are fine
    args += ["-c:v", v_codec]
    args += list(extra_video or ("-preset", "veryfast", "-crf", "18"))
    args += ["-pix_fmt", "yuv420p", out]
    run(args)
    return out


def overlays_from_project(project: Any) -> list[dict[str, Any]]:
    """Project overlay records -> composite_overlays() inputs.

    Overlay anchors are ``{segment_id, offset_frames}``, never absolute
    positions, so the timeline position has to be read out of ``_computed``:
    a cut six frames upstream must move the graphic, not orphan it.
    """
    # Project keeps the document on .data; reaching for the wrong attribute
    # returns the Project itself, which still answers .get() and would leave
    # every overlay silently unanchored.
    data = getattr(project, "data", None)
    if not isinstance(data, dict):
        data = project
    positions = (data.get("_computed") or {}).get("positions") or {}
    out: list[dict[str, Any]] = []
    for ov in data.get("overlays") or []:
        render = ov.get("render") or {}
        path = render.get("path")
        if not path:
            continue
        anchor = ov.get("anchor") or {}
        pos = positions.get(anchor.get("segment_id"))
        if pos is None:
            raise GfxError(
                f"overlay {ov.get('id')} anchors to segment {anchor.get('segment_id')!r}, "
                "which is not in _computed.positions (disabled, or the project was not recomputed)"
            )
        out.append(
            {
                "id": ov.get("id"),
                "path": project.resolve(path) if hasattr(project, "resolve") else path,
                "tl_in_frames": int(pos["tl_in"]) + int(anchor.get("offset_frames", 0)),
                "x": ov.get("x", 0),
                "y": ov.get("y", 0),
                "layer": int(ov.get("layer", 0)),
            }
        )
    return out


__all__ = [
    "GfxError",
    "chrome_exe",
    "chrome_version",
    "chrome_screenshot",
    "assert_native_windows_path",
    "file_url",
    "blank_hash",
    "template_fingerprint",
    "resolve_template",
    "frame_params",
    "render_html_frames",
    "stage_frames",
    "frames_to_alpha_movie",
    "probe_alpha",
    "frame_hashes",
    "render_overlay",
    "composite_overlays",
    "overlays_from_project",
]
