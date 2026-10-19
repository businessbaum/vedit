"""The render core: per-segment video cache, guarded concat, one mux.

CONTRACT section 6 is settled architecture. The shape, and why each half is the
way it is:

- **Video** is cached per segment and joined with ``-f concat -safe 0 -c copy``.
  Stream-copy CUTTING is not frame accurate (a 75-frame request came back as 92
  because the in-point snapped to the previous keyframe), but stream-copy
  JOINING of segments produced by identical encode settings is lossless and
  near-instant, because each cached segment starts on an IDR by construction.
- **Audio** is never cached per segment. See ``audio.py``.
- The concat demuxer does NOT validate its inputs. Joining 1280x720 with
  640x360 exited 0, produced a corrupt file and advertised 1280x720 for the
  whole thing. Every join in this module goes through ``concat_compatible()``
  first, and a failure is an exception, not a warning.

The cache key is the load-bearing detail. It contains EXACTLY the fields that
change the segment's decoded pixels, and nothing else -- no timeline position,
no segment id, no neighbour, no ``enabled``/``locked``/``provenance``. Put any
of those in and the first trivial edit invalidates all 180 segments, which
turns the one feature that makes this pipeline worth building into a slower
version of re-rendering from scratch.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .audio import loudnorm_two_pass, render_audio
from .cache import Cache
from .ffmpeg import FFMPEG, VeditError, filter_complex_args, run, write_filtergraph
from .keys import cache_key, strong_key
from .probe import concat_compatible, count_frames, probe
from .project import Project
from .timebase import Timebase

# Bump to invalidate every cached segment at once (an encoder-args bug fix, a
# change to the chain this module emits). It is cheaper than reasoning about
# which old artifacts are still trustworthy.
CACHE_SCHEMA_VERSION = 3

SEG_EXT = ".mkv"

# Matroska for cache segments: no moov atom, so a killed render leaves a file
# that is short rather than one that is unreadable, and the demuxer does not
# care about faststart when joining.

_NVENC = ("h264_nvenc", "hevc_nvenc", "av1_nvenc")


class RenderError(VeditError):
    """A render step failed, or refused to run because it would be wrong."""


@dataclass
class RenderStats:
    """What actually happened. The proof that the cache did its job."""

    hits: int = 0
    misses: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    concat_ms: int = 0
    audio_ms: int = 0
    loudnorm_ms: int = 0
    mux_ms: int = 0

    def record(self, seg_id: str, key: str, hit: bool, ms: int, frames: int, path: str) -> None:
        self.segments.append(
            {"id": seg_id, "key": key, "hit": hit, "ms": ms, "frames": frames, "path": path}
        )
        if hit:
            self.hits += 1
        else:
            self.misses += 1

    @property
    def render_ms(self) -> int:
        return sum(s["ms"] for s in self.segments if not s.get("hit"))

    @property
    def unexpected_misses(self) -> list[str]:
        """Segments outside ``only=`` that were not in the cache after all."""
        return [s["id"] for s in self.segments if s.get("unexpected_miss")]

    def summary(self) -> str:
        return (
            f"{self.hits} hit / {self.misses} miss, "
            f"{self.render_ms} ms encoding, {self.concat_ms} ms concat, "
            f"{self.audio_ms} ms audio, {self.loudnorm_ms} ms loudnorm, {self.mux_ms} ms mux"
        )


# --- tool identity ------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def nvidia_driver_version() -> str:
    """The NVIDIA driver version, for NVENC cache keys.

    NVENC is byte-reproducible run to run on one machine but is NOT
    contractually stable across driver updates. The ffmpeg version covers the
    ffmpeg side; this covers the other half. Only added to the key when the
    encoder is actually NVENC, so a libx264 project's cache survives a driver
    update untouched.
    """
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    first = (proc.stdout or "").strip().splitlines()
    return first[0].strip() if first else "unknown"


@functools.lru_cache(maxsize=1)
def _ffmpeg_version() -> str:
    from .ffmpeg import tool_versions

    return tool_versions().get("ffmpeg", "unknown")


# --- encoder ------------------------------------------------------------------


def _encoder(output_cfg: dict, tb: Timebase) -> tuple[list[str], dict[str, Any]]:
    """(argv fragment, key fragment) for the video encoder.

    The key fragment must name every option that changes the produced bytes.
    Anything omitted here is an option you can change without getting a
    re-render, which is a silently wrong output file.
    """
    codec = str(output_cfg.get("v_codec", "libx264"))
    preset = str(output_cfg.get("preset", "p6" if codec in _NVENC else "medium"))
    cq = int(output_cfg.get("cq", 20))
    # One IDR per second. Cached segments each start on an IDR anyway (a fresh
    # encode always does), so this only bounds seek granularity in the master.
    gop = int(output_cfg.get("gop") or round(tb.fps))
    extra = [str(x) for x in (output_cfg.get("encoder_extra") or [])]

    if codec in _NVENC:
        argv = [
            "-c:v",
            codec,
            "-preset",
            preset,
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(cq),
            # -b:v 0 is REQUIRED or -cq is silently ignored and NVENC falls back
            # to its default bitrate.
            "-b:v",
            "0",
            "-g",
            str(gop),
            # Without forced-idr a cached segment can open on a non-IDR frame
            # and the concat demuxer then produces visible corruption at that
            # junction with exit 0.
            "-forced-idr",
            "1",
        ]
        if codec == "h264_nvenc":
            argv += ["-profile:v", str(output_cfg.get("profile", "high"))]
        if codec == "hevc_nvenc":
            argv += ["-tag:v", "hvc1"]
    else:
        argv = ["-c:v", codec, "-preset", preset, "-crf", str(cq), "-g", str(gop)]
        if codec == "libx264":
            # Closed GOP with no scene-cut IDRs: the only way a cached segment's
            # first frame is guaranteed to be the only IDR the joiner relies on.
            argv += ["-x264-params", "scenecut=0:open_gop=0"]
    argv += extra

    key = {
        "c": codec,
        "preset": preset,
        "cq": cq,
        "g": gop,
        "extra": extra,
    }
    if codec == "libx264":
        key["x264_params"] = "scenecut=0:open_gop=0"
    if codec == "h264_nvenc":
        key["profile"] = str(output_cfg.get("profile", "high"))
    return argv, key


def _video_chain(seg: dict, src_info: dict, out_w: int, out_h: int, pix_fmt: str, tb: Timebase) -> str:
    """The fully resolved per-segment video filtergraph. Goes into the key verbatim.

    Contains no path, no timeline position and no segment identity -- only
    things that change pixels. That is what makes it safe to hash.
    """
    speed = float(seg.get("speed", 1.0) or 1.0)
    parts: list[str] = []
    if speed != 1.0:
        parts.append(f"setpts=PTS/{speed:g}")
        # Retiming produces non-integer PTS steps; force the grid back.
        parts.append(f"fps=fps={tb.fps_num}/{tb.fps_den}")
    if src_info.get("width") and (int(src_info["width"]) != out_w or int(src_info["height"]) != out_h):
        parts.append(f"scale={out_w}:{out_h}:flags=lanczos")
    # concat_compatible() checks width/height/pix_fmt but NOT sample aspect;
    # a SAR mismatch between segments survives the guard and reaches the player.
    parts.append("setsar=1")
    parts.append(f"format={pix_fmt}")
    return "[0:v]" + ",".join(parts) + "[v]"


# --- one segment --------------------------------------------------------------


def segment_cache_key(project: Project, seg: dict) -> tuple[str, dict[str, Any]]:
    """(key, payload). Exposed so a cache miss can be explained by diffing.

    The payload is EXACTLY the research's key fields plus the tool versions.
    Adding anything positional here destroys the cache on every trivial edit.
    """
    tb = project.timebase()
    out = project.data.get("output") or {}
    src = project.source(str(seg["source_id"]))
    path = project.resolve(str(src["path"]))

    source_key = src.get("strong_key")
    if not source_key:
        source_key = strong_key(path)
        src["strong_key"] = source_key

    res = out.get("resolution") or [1920, 1080]
    out_w, out_h = int(res[0]), int(res[1])
    pix_fmt = str(out.get("pix_fmt", "yuv420p"))

    src_info = dict(src.get("probe") or {})
    if not src_info.get("width"):
        src_info = probe(path)

    enc_argv, enc_key = _encoder(out, tb)
    chain = _video_chain(seg, src_info, out_w, out_h, pix_fmt, tb)

    tool: dict[str, Any] = {"ffmpeg": _ffmpeg_version()}
    if enc_key["c"] in _NVENC:
        tool["nvidia_driver"] = nvidia_driver_version()

    payload = {
        "v": CACHE_SCHEMA_VERSION,
        "source_key": source_key,
        "src_in_frames": int(seg["src_in_frames"]),
        "src_out_frames": int(seg["src_out_frames"]),
        "timebase": [tb.fps_num, tb.fps_den],
        "out_resolution": [out_w, out_h],
        "pix_fmt": pix_fmt,
        "speed": float(seg.get("speed", 1.0) or 1.0),
        "filtergraph": chain,
        "encoder": enc_key,
        "tool": tool,
    }
    return cache_key(payload), payload


def render_segment(
    project: Project,
    seg: dict,
    cache: Cache,
    *,
    stats: RenderStats | None = None,
    force: bool = False,
) -> str:
    """Render one segment to the cache and return its path. Cached.

    ``-ss`` goes BEFORE ``-i``: measured frame-accurate (the burned-in frame
    number matched) and 5.5x faster than after the input. ``-frames:v`` rather
    than ``-t`` because the frame count is the thing QC check (a) compares
    against, so it is the thing that should be commanded directly.

    Video only (``-an``). Audio is one pass over the whole timeline; a cached
    audio segment would drift at every junction.
    """
    tb = project.timebase()
    seg_id = str(seg.get("id"))
    key, payload = segment_cache_key(project, seg)
    frames = Project.segment_frames(seg)
    if frames <= 0:
        raise RenderError(f"segment {seg_id!r} is empty")

    started = time.perf_counter()
    if not force and cache.has("seg", key, SEG_EXT, expect_frames=frames):
        path = cache.path_for("seg", key, SEG_EXT)
        ms = int((time.perf_counter() - started) * 1000)
        _write_back(seg, key, frames, path)
        if stats is not None:
            stats.record(seg_id, key, True, ms, frames, path)
        return path

    src = project.source(str(seg["source_id"]))
    src_path = project.resolve(str(src["path"]))
    if not os.path.isfile(src_path):
        raise RenderError(f"segment {seg_id!r} points at a missing source: {src_path}")

    out_cfg = project.data.get("output") or {}
    enc_argv, _ = _encoder(out_cfg, tb)

    work = os.path.join(cache.root, "fc")
    fc = write_filtergraph(payload["filtergraph"] + "\n", work)
    tmp = cache.tmp_for("seg", key, SEG_EXT)

    argv = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-nostats",
        "-ss",
        tb.secs(int(seg["src_in_frames"])),
        "-i",
        src_path,
    ]
    argv += filter_complex_args(fc)
    argv += ["-map", "[v]", "-an", "-frames:v", str(frames), "-fps_mode", "cfr"]
    argv += enc_argv
    argv += [tmp]

    try:
        run(argv)
        actual = count_frames(tmp)
        if actual != frames:
            raise RenderError(
                f"segment {seg_id!r} rendered {actual} frames, expected {frames} "
                f"(src {seg['src_in_frames']} -> {seg['src_out_frames']})"
            )
        path = cache.commit(
            tmp,
            "seg",
            key,
            SEG_EXT,
            {
                **payload,
                "frames": frames,
                "render_ms": int((time.perf_counter() - started) * 1000),
            },
        )
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    ms = int((time.perf_counter() - started) * 1000)
    _write_back(seg, key, frames, path)
    if stats is not None:
        stats.record(seg_id, key, False, ms, frames, path)
    return path


def _write_back(seg: dict, key: str, frames: int, path: str) -> None:
    """Record the key on the segment. Not part of the key itself, obviously."""
    seg["render"] = {
        "cache_key": key,
        "frames": frames,
        "path": path,
        "rendered_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# --- joining ------------------------------------------------------------------


def _concat_list(paths: Sequence[str], list_path: str) -> str:
    """Write the demuxer's list file.

    Absolute Windows paths work in both slash directions (verified). Inside the
    single quotes the demuxer's own escape for a quote is ``'\\''`` -- a
    filename containing an apostrophe is otherwise a truncated path and a
    confusing "No such file" from a line that looks correct.
    """
    os.makedirs(os.path.dirname(os.path.abspath(list_path)), exist_ok=True)
    with open(list_path, "w", encoding="utf-8", newline="\n") as fh:
        for p in paths:
            safe = os.path.abspath(p).replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")
    return list_path


def concat_segments(
    paths: list[str],
    out_path: str,
    *,
    verify: bool = True,
    expect_frames: int | None = None,
) -> str:
    """Join cached segments with ``-c copy``. GUARDED. Returns ``out_path``.

    The guard is not optional and not a warning: on a parameter mismatch the
    concat demuxer writes a corrupt file and exits 0, so the only place this can
    be caught is before it runs.

    ``expect_frames`` lets a caller that already knows the total (render_all
    does -- it is Sigma(out-in) over the enabled segments) skip decoding every
    input just to re-derive it. Without it the check costs a full decode of the
    inputs AND the output, which on a 20-minute timeline is the most expensive
    thing in the pipeline that produces no pixels.
    """
    if not paths:
        raise RenderError("nothing to concatenate")
    ok, reason = concat_compatible(list(paths))
    if not ok:
        raise RenderError(f"refusing to -c copy concat: {reason}")

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    work = os.path.join(os.path.dirname(out_path), ".work")
    list_path = _concat_list(paths, os.path.join(work, "concat_list.txt"))

    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-nostats",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            out_path,
        ]
    )

    if verify:
        expected = expect_frames if expect_frames is not None else sum(count_frames(p) for p in paths)
        actual = count_frames(out_path)
        if actual != expected:
            raise RenderError(
                f"concat produced {actual} frames, expected {expected} "
                f"(the demuxer exits 0 on a bad join, so this check is the only signal)"
            )
    return out_path


def mux(video_path: str, audio_path: str, out_path: str, output_cfg: dict) -> str:
    """Mux the joined video against the single-pass audio. AAC encoded ONCE.

    ``-c:v copy``: the video was already encoded, once, per segment. Re-encoding
    here would throw away the entire per-segment cache at the last step.
    """
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    a_codec = str(output_cfg.get("a_codec", "aac"))
    a_bitrate = str(output_cfg.get("a_bitrate", "320k"))
    channels = int(output_cfg.get("a_channels", 2) or 2)
    rate = int((output_cfg.get("sample_rate") or 48000))

    argv = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-nostats",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        a_codec,
        "-b:a",
        a_bitrate,
        "-ar",
        str(rate),
        "-ac",
        str(channels),
    ]
    if out_path.lower().endswith((".mp4", ".m4v", ".mov")):
        argv += ["-movflags", "+faststart"]
    argv += [out_path]
    run(argv)
    return out_path


# --- the whole thing ----------------------------------------------------------


def render_all(
    project: Project,
    *,
    only: Iterable[str] | str | None = None,
    stats: RenderStats | None = None,
    loudnorm: bool = True,
    save: bool = True,
    verify_concat: bool = True,
) -> str:
    """Render the project to ``output.path``. Returns the absolute output path.

    Re-renders ONLY what changed: a segment whose key still matches a cached
    artifact is a file-existence check plus one ffprobe, not an encode. That is
    the whole payoff -- "move that cut 200 ms earlier" touches one integer,
    which changes one key, which re-renders one segment out of ~180.

    ``only`` restricts encoding to the named segment ids. Everything else is
    taken from the cache; a segment outside ``only`` that is NOT cached is still
    rendered (the alternative is a silently incomplete master) and reported as
    an unexpected miss.
    """
    if stats is None:
        stats = RenderStats()
    only_set: set[str] | None
    if only is None:
        only_set = None
    elif isinstance(only, str):
        only_set = {only}
    else:
        only_set = {str(x) for x in only}

    project.recompute()
    segs = project.enabled_segments()
    if not segs:
        raise RenderError("no enabled segments to render")

    out_cfg = project.data.get("output") or {}
    out_path = project.resolve(str(out_cfg.get("path", "out/final.mp4")))
    work = os.path.join(project.dir, "out", ".work")
    os.makedirs(work, exist_ok=True)

    cache = Cache()
    try:
        paths: list[str] = []
        for seg in segs:
            seg_id = str(seg.get("id"))
            if only_set is not None and seg_id not in only_set:
                key, _ = segment_cache_key(project, seg)
                frames = Project.segment_frames(seg)
                if cache.has("seg", key, SEG_EXT, expect_frames=frames):
                    path = cache.path_for("seg", key, SEG_EXT)
                    _write_back(seg, key, frames, path)
                    stats.record(seg_id, key, True, 0, frames, path)
                    paths.append(path)
                    continue
                # Outside `only=` and not cached. Rendering it anyway beats
                # shipping a master with a hole in it; the flag says it happened.
                path = render_segment(project, seg, cache, stats=stats)
                stats.segments[-1]["unexpected_miss"] = True
                paths.append(path)
                continue
            paths.append(render_segment(project, seg, cache, stats=stats))

        video_only = os.path.join(work, "video_only.mkv")
        t0 = time.perf_counter()
        concat_segments(
            paths,
            video_only,
            verify=verify_concat,
            expect_frames=sum(Project.segment_frames(s) for s in segs),
        )
        stats.concat_ms = int((time.perf_counter() - t0) * 1000)

        audio_raw = os.path.join(work, "audio_raw.wav")
        t0 = time.perf_counter()
        render_audio(project, audio_raw)
        stats.audio_ms = int((time.perf_counter() - t0) * 1000)

        audio_final = audio_raw
        if loudnorm and out_cfg.get("loudnorm"):
            audio_final = os.path.join(work, "audio_norm.wav")
            t0 = time.perf_counter()
            target = dict(out_cfg["loudnorm"])
            target.setdefault("sample_rate", project.timebase().sample_rate)
            loudnorm_two_pass(audio_raw, audio_final, target)
            stats.loudnorm_ms = int((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        mux(video_only, audio_final, out_path, out_cfg)
        stats.mux_ms = int((time.perf_counter() - t0) * 1000)
    finally:
        cache.close()

    if save:
        project.save()
    return out_path


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "SEG_EXT",
    "RenderError",
    "RenderStats",
    "nvidia_driver_version",
    "segment_cache_key",
    "render_segment",
    "concat_segments",
    "mux",
    "render_all",
]
