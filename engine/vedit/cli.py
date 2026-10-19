"""``ve`` -- the surface a human and Claude actually touch.

Design rules for this module:

- **Every subcommand is a thin driver over a library function.** The CLI decides
  what to say, never how to cut or render. Anything worth calling twice lives in
  a ``vedit`` module.
- **Stages that are still being built fail loudly and specifically.** The
  pipeline lands module by module; ``ve cut`` on a tree without ``vedit/cut.py``
  says exactly which import is missing rather than dying in a traceback.
- **Nothing is ever deleted.** ``ve cut`` disables segments, it does not drop
  them. ``ve intake`` adds sources, it never rewrites one.
- **Exit codes mean something.** 0 success, 1 error, and for ``ve qc`` the QC
  contract's 0/1/2.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as _dt
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

from .cache import Cache
from .ffmpeg import FFMPEG, VeditError, run
from .keys import repo_root, strong_key
from .probe import count_frames, is_vfr, probe
from .project import Project, ProjectError
from .qc import format_findings, run_qc, segment_cache_path
from .timebase import Timebase

PROG = "ve"

# Platform -> loudness target. RESEARCH DEFAULTS, not measured on this machine:
# the delivery targets are published by the platforms, but nothing here has been
# A/B'd against a real upload. `ve qc` compares the master against whatever ends
# up in project.json, so changing a number here changes the QC target too.
PLATFORM_LOUDNESS: dict[str, dict[str, float]] = {
    "youtube": {"I": -14.0, "TP": -1.5, "LRA": 11.0},
    "tiktok": {"I": -14.0, "TP": -1.0, "LRA": 9.0},
    "instagram": {"I": -14.0, "TP": -1.0, "LRA": 9.0},
    "linkedin": {"I": -14.0, "TP": -1.5, "LRA": 11.0},
}
PLATFORMS = tuple(PLATFORM_LOUDNESS)

# Cut thresholds live in vedit/cut.py and NOWHERE else. The CLI only assembles
# preset overrides on top of them. Restating contract section 8 here would
# recreate exactly the "defined in three places, specified in none" problem this
# pipeline was built to end.

_BUILTIN_FORMATS: dict[str, dict[str, Any]] = {
    "longform": {
        "name": "longform",
        "timebase": {"fps_num": 30, "fps_den": 1, "sample_rate": 48000},
        "output": {"resolution": [1920, 1080], "platform": "youtube"},
        "captions": {"preset": "longform-clean"},
    },
    "shortform-explainer": {
        "name": "shortform-explainer",
        "timebase": {"fps_num": 30, "fps_den": 1, "sample_rate": 48000},
        "output": {"resolution": [1080, 1920], "platform": "tiktok"},
        "captions": {"enabled": True, "preset": "shortform-highlight"},
    },
    "tiktok-raw": {
        "name": "tiktok-raw",
        "timebase": {"fps_num": 30, "fps_den": 1, "sample_rate": 48000},
        "output": {"resolution": [1080, 1920], "platform": "tiktok"},
        "captions": {"enabled": True, "preset": "shortform-highlight"},
    },
}


class CliError(Exception):
    """A user-facing error. Printed as one line, never as a traceback."""


class StageNotReady(CliError):
    """A pipeline module this subcommand needs is not on disk yet."""


# --- small utilities ----------------------------------------------------------


def _root(args: argparse.Namespace) -> str:
    return os.path.abspath(getattr(args, "root", None) or os.environ.get("VEDIT_ROOT") or repo_root())


def _project_dir(root: str, slug: str) -> str:
    return os.path.join(root, "projects", slug)


def _load(args: argparse.Namespace) -> Project:
    root = _root(args)
    path = os.path.join(_project_dir(root, args.slug), "project.json")
    if not os.path.isfile(path):
        available = _list_slugs(root)
        hint = f" Known projects: {', '.join(available)}" if available else " No projects exist yet; try `ve new <slug>`."
        raise CliError(f"no project {args.slug!r} at {path}.{hint}")
    return Project.load(path)


def _list_slugs(root: str) -> list[str]:
    base = os.path.join(root, "projects")
    if not os.path.isdir(base):
        return []
    return sorted(
        name for name in os.listdir(base) if os.path.isfile(os.path.join(base, name, "project.json"))
    )


def _timecode(frames: int, tb: Timebase) -> str:
    """Frames -> H:MM:SS.s. For reading, never for arithmetic."""
    total = frames * tb.fps_den / tb.fps_num
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{int(hours)}:{int(minutes):02d}:{seconds:04.1f}"
    return f"{int(minutes)}:{seconds:04.1f}"


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _import(modname: str, why: str):
    """Import ``vedit.<modname>`` or explain, precisely, what is missing.

    The pipeline is built module by module by several hands. A traceback that
    says ``ModuleNotFoundError: vedit.cut`` costs a minute of reading; this costs
    none.
    """
    import importlib

    try:
        return importlib.import_module(f".{modname}", package=__package__)
    except ImportError as exc:
        raise StageNotReady(
            f"{why} needs engine/vedit/{modname}.py, which is not importable yet ({exc}). "
            f"That module is part of the pipeline build; everything else still works."
        ) from exc


def _entry(module, names: Sequence[str], why: str) -> Callable:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    raise StageNotReady(
        f"{why}: {module.__name__} defines none of {', '.join(names)}. "
        f"It has: {', '.join(sorted(n for n in dir(module) if not n.startswith('_')))[:300]}"
    )


def _call_flexible(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` dropping keyword arguments it does not accept.

    The contract fixes signatures like ``render_all(project, *, only=None)``.
    ``--draft`` is a CLI nicety that a conforming implementation is not obliged
    to support, and passing it must not be a TypeError.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if not accepts_kwargs:
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(*args, **kwargs)


def _safe_stem(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-")
    return stem[:48] or "src"


def _rel_to_project(proj: Project, path: str) -> str:
    """Absolute path -> the project-relative spelling stored in project.json.

    Project-relative is what lets a project directory be moved or copied without
    rewriting every path in it. An absolute path survives as-is when it is on
    another drive, where a relative path cannot be expressed at all.
    """
    path = os.path.abspath(path)
    try:
        rel = os.path.relpath(path, proj.dir)
    except ValueError:
        return path.replace("\\", "/")
    if rel.startswith(".." + os.sep + ".." + os.sep + ".." + os.sep + ".."):
        return path.replace("\\", "/")
    return rel.replace("\\", "/")


def _preset_dirs(root: str, kind: str) -> list[str]:
    """Where to look for a preset: the active root first, then the engine's repo.

    ``--root`` relocates the PROJECT tree, not the shared library. presets/ and
    assets/fonts/ belong to the engine (qc.py already resolves fonts through
    repo_root()), so a root override that had to carry its own copy of every
    preset would make the flag useless for anything but a full second checkout.
    """
    dirs = [os.path.join(root, "presets", kind)]
    engine_repo = os.path.join(repo_root(), "presets", kind)
    if os.path.normcase(engine_repo) != os.path.normcase(dirs[0]):
        dirs.append(engine_repo)
    return dirs


def _load_format_preset(root: str, name: str) -> dict[str, Any]:
    for directory in _preset_dirs(root, "formats"):
        path = os.path.join(directory, f"{name}.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError as exc:
            raise CliError(f"{path} is not valid JSON: {exc}") from exc
        data.setdefault("name", name)
        return data
    if name in _BUILTIN_FORMATS:
        # A missing preset file is not fatal: the built-in matches the contract's
        # own example so `ve new` works on a fresh clone before presets land.
        return dict(_BUILTIN_FORMATS[name])
    known = sorted(set(_BUILTIN_FORMATS) | set(_disk_presets(root)))
    raise CliError(f"unknown format preset {name!r}; known: {', '.join(known)}")


def _disk_presets(root: str, kind: str = "formats") -> list[str]:
    names: list[str] = []
    for d in _preset_dirs(root, kind):
        if os.path.isdir(d):
            names += [os.path.splitext(n)[0] for n in os.listdir(d) if n.endswith(".json")]
    return sorted(set(names))


# --- ve new -------------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> int:
    root = _root(args)
    preset = _load_format_preset(root, args.preset)

    preset_platform = (preset.get("output") or {}).get("platform")
    platform = args.platform or preset_platform or "youtube"
    if platform not in PLATFORM_LOUDNESS:
        raise CliError(f"unknown platform {platform!r}; known: {', '.join(PLATFORMS)}")
    output = dict(preset.get("output") or {})
    output["platform"] = platform
    # The preset's own loudnorm wins while it is still describing the platform
    # it was written for -- a preset author may have tuned it. Switching
    # platform on the command line invalidates that and the table takes over.
    if platform != preset_platform or not output.get("loudnorm"):
        output["loudnorm"] = dict(PLATFORM_LOUDNESS[platform])
    preset = dict(preset)
    preset["output"] = output

    try:
        proj = Project.create(root, args.slug, preset)
    except ProjectError as exc:
        raise CliError(str(exc)) from exc

    res = (proj.get("output") or {}).get("resolution")
    tb = proj.timebase()
    print(f"created {proj.path}")
    print(
        f"  preset {proj.get('format_preset')}  platform {platform}  "
        f"{res[0]}x{res[1]} @ {tb.fps_num}/{tb.fps_den} fps  {tb.sample_rate} Hz"
    )
    print(f"  loudness target I={output['loudnorm']['I']} TP={output['loudnorm']['TP']} LRA={output['loudnorm']['LRA']}")
    print(f"  next: {PROG} intake {args.slug} <media...>")
    return 0


# --- ve intake ----------------------------------------------------------------


def _next_source_id(proj: Project) -> str:
    used = {str(s.get("id")) for s in proj.get("sources") or []}
    for i in range(26 * 27):
        # A..Z then AA..AZ. Single letters first because they read better in
        # segment provenance than "src_00".
        label = chr(ord("A") + i) if i < 26 else chr(ord("A") + i // 26 - 1) + chr(ord("A") + i % 26)
        if label not in used:
            return label
    raise CliError("too many sources")


def _normalize_source(src: str, dst: str, tb: Timebase, info: dict, *, quiet: bool) -> None:
    """Mandatory CFR transcode (CONTRACT section 0.2).

    Not optional and not "recommended". Normalizing at the door kills the whole
    fps-scaling, drop-frame and VFR bug class at once -- ffprobe and every other
    tool disagree about the frame count of a VFR mp4, which alone makes VFR
    intake untenable.

    Sources without audio get silence rather than no audio track: the concat
    demuxer refuses to notice a stream-layout mismatch and writes a corrupt file
    with exit 0, so every normalized source must have the same layout.

    The transcode is BITEXACT. Without it the Matroska muxer stamps a random
    SegmentUID and the wall clock into every file, so intaking one raw clip into
    two projects produced two normalized files of identical size and different
    sha256 -- measured, 67bd22c7 vs 6542a201. Every downstream key hangs off that
    hash (segment renders, the transcript sidecar), so two projects sharing a
    B-roll clip shared nothing, which is exactly the case the global
    content-addressed cache exists for. -bitexact touches only container and
    encoder metadata; the decoded pixels and samples are unchanged.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    gop = max(1, int(round(tb.fps)))
    argv: list[str] = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-fflags", "+bitexact", "-i", src]
    if not info.get("has_audio"):
        argv += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={tb.sample_rate}"]
    argv += ["-map", "0:v:0", "-map", ("1:a:0" if not info.get("has_audio") else "0:a:0")]
    if not info.get("has_audio"):
        argv += ["-shortest"]
    argv += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr", "-r", f"{tb.fps_num}/{tb.fps_den}",
        "-g", str(gop), "-keyint_min", str(gop),
        # pcm_s16le, in Matroska: a mezzanine that never accumulates codec
        # priming. AAC would add ~1024 samples of head padding per re-encode.
        "-c:a", "pcm_s16le", "-ar", str(tb.sample_rate), "-ac", "2",
        "-af", "aresample=resampler=soxr:precision=28",
        "-map_metadata", "-1",
        # See the docstring: this is what makes the normalized file, and every
        # cache key derived from it, reproducible across projects and machines.
        "-flags", "+bitexact", "-fflags", "+bitexact",
        dst,
    ]
    if not quiet:
        print(f"    transcoding -> {os.path.basename(dst)} (CFR {tb.fps_num}/{tb.fps_den}, {tb.sample_rate} Hz)")
    run(argv)


def cmd_intake(args: argparse.Namespace) -> int:
    proj = _load(args)
    tb = proj.timebase()
    sources = proj.data.setdefault("sources", [])
    known_keys = {s.get("strong_key") for s in sources}

    out_res = (proj.get("output") or {}).get("resolution") or [1920, 1080]
    added = 0

    for raw in args.media:
        src = os.path.abspath(raw)
        if not os.path.isfile(src):
            raise CliError(f"no such file: {src}")
        print(f"  {os.path.basename(src)}")
        info = probe(src)
        if not info.get("has_video"):
            raise CliError(f"{src} has no video stream; music goes in with `{PROG} music`")

        key = strong_key(src)
        if key in known_keys:
            print(f"    already registered (strong key {key.split(':')[-1][:12]}); skipped")
            continue

        sid = _next_source_id(proj)
        dst = os.path.join(proj.dir, "media", f"{sid}_{_safe_stem(src)}.mkv")
        if os.path.exists(dst) and not args.force:
            raise CliError(f"{dst} already exists; pass --force to overwrite")

        if info.get("is_vfr"):
            print("    source is VFR -- normalizing to CFR is exactly why intake transcodes")
        if [info.get("width"), info.get("height")] != list(out_res):
            print(
                f"    note: {info.get('width')}x{info.get('height')} differs from the project's "
                f"{out_res[0]}x{out_res[1]}; the render lane scales, intake does not"
            )

        _normalize_source(src, dst, tb, info, quiet=args.quiet)

        norm = probe(dst)
        frames = count_frames(dst)
        record = {
            "id": sid,
            "path": _rel_to_project(proj, dst),
            "original_path": src.replace("\\", "/"),
            "role": args.role,
            "strong_key": strong_key(dst),
            "probe": {
                "duration_frames": frames,
                "fps_num": norm["fps_num"],
                "fps_den": norm["fps_den"],
                "sample_rate": norm["sample_rate"],
                "channels": norm["channels"],
                "v_codec": norm["v_codec"],
                "a_codec": norm["a_codec"],
                "width": norm["width"],
                "height": norm["height"],
            },
            "original": {
                "strong_key": key,
                "fps_num": info["fps_num"],
                "fps_den": info["fps_den"],
                "is_vfr": bool(info.get("is_vfr")),
                "duration_s": info.get("duration_s"),
            },
        }
        sources.append(record)
        known_keys.add(key)
        added += 1
        print(f"    source {sid}: {frames} frames = {_timecode(frames, tb)}  {norm['v_codec']}/{norm['a_codec']}")

        if (norm["fps_num"], norm["fps_den"]) != (tb.fps_num, tb.fps_den):
            raise CliError(
                f"intake produced {norm['fps_num']}/{norm['fps_den']} but the project timebase is "
                f"{tb.fps_num}/{tb.fps_den}. Refusing to register a source on the wrong timebase."
            )

    if added:
        proj.set_stage("intake")
        proj.save()
    print(f"{added} source(s) added; {len(sources)} total. next: {PROG} transcribe {args.slug}")
    return 0


# --- ve transcribe ------------------------------------------------------------

_ASR_DRIVER_CANDIDATES = (
    os.path.join("tools", "asr", "transcribe.py"),
    os.path.join("tools", "asr_transcribe.py"),
    os.path.join("engine", "asr", "transcribe.py"),
    os.path.join("tools", "transcribe.py"),
)


def _asr_python(root: str) -> str:
    exe = os.path.join(root, ".venv-asr", "Scripts", "python.exe")
    if not os.path.isfile(exe):
        raise StageNotReady(
            f"no ASR environment at {exe}. Run `tools\\setup.ps1` -- it creates .venv-asr with "
            f"uv --python 3.12 and installs whisperx (several GB)."
        )
    return exe


def _asr_driver(root: str) -> str:
    for rel in _ASR_DRIVER_CANDIDATES:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            return path
    raise StageNotReady(
        "no ASR driver script found. Expected one of: " + ", ".join(_ASR_DRIVER_CANDIDATES) + ". "
        "It must accept --input <media> --out <words.json> --language <code> and write word-level JSON."
    )


def cmd_transcribe(args: argparse.Namespace) -> int:
    proj = _load(args)
    root = _root(args)
    sources = proj.get("sources") or []
    if not sources:
        raise CliError(f"no sources; run `{PROG} intake {args.slug} <media...>` first")

    if args.source:
        source = proj.source(args.source)
    else:
        source = next((s for s in sources if s.get("role") == "acam"), sources[0])

    media = proj.resolve(source["path"])
    key = str(source.get("strong_key") or strong_key(media))
    cache = Cache()
    words_path = cache.path_for("transcript", key, ".words.json")

    if os.path.isfile(words_path) and not args.force:
        print(f"transcript already cached for source {source['id']}: {words_path}")
    else:
        py = _asr_python(root)
        driver = _asr_driver(root)
        os.makedirs(os.path.dirname(words_path), exist_ok=True)
        argv = [
            py, driver,
            "--input", media,
            "--out", words_path,
            "--language", args.language,
        ]
        if args.model:
            argv += ["--model", args.model]
        print(f"transcribing source {source['id']} ({args.language}) via {os.path.relpath(driver, root)}")
        # The ASR venv needs torch's DLL directory on PATH; tools/ve.ps1 does
        # that for the parent process and the child inherits it. Setting it here
        # too keeps `python engine/ve.py` working without the wrapper.
        env = dict(os.environ)
        torch_lib = os.path.join(root, ".venv-asr", "Lib", "site-packages", "torch", "lib")
        if os.path.isdir(torch_lib) and torch_lib not in env.get("PATH", ""):
            env["PATH"] = torch_lib + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, check=False)
        if proc.returncode != 0:
            raise CliError(f"ASR driver exited {proc.returncode}")
        if not os.path.isfile(words_path):
            raise CliError(f"ASR driver exited 0 but wrote nothing to {words_path}")

    transcript_mod = _import("transcript", "ve transcribe")
    with open(words_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "segments" in raw:
        # Raw WhisperX output: normalize once and rewrite in place so every later
        # stage reads one shape.
        normalize = _entry(transcript_mod, ("normalize_whisperx",), "ve transcribe")
        words = normalize(raw)
        with open(words_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(words, fh, ensure_ascii=False)
    else:
        load_words = _entry(transcript_mod, ("load_words",), "ve transcribe")
        words = load_words(words_path)

    meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
    proj.data["transcript"] = {
        "source_id": source["id"],
        "path": _rel_to_project(proj, words_path),
        "engine": meta.get("engine", "whisperx-3.8.6"),
        "align_model": meta.get("align_model", "VOXPOPULI_ASR_BASE_10K_DE" if args.language == "de" else ""),
        "language": args.language,
        "timestamp_source": meta.get("timestamp_source", "wav2vec2-forced-align"),
        "word_count": len(words),
    }
    proj.set_stage("transcribed")
    proj.save()
    print(f"{len(words)} words -> {words_path}")
    print(f"  next: story pass (Claude reads the transcript), then `{PROG} cut {args.slug}`")
    return 0


# --- ve probe -----------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    """Probe a file before intaking it. Reads nothing from a project."""
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise CliError(f"no such file: {path}")
    info = dict(probe(path))
    if args.json:
        _emit(info)
        return 0
    print(path)
    fps = f"{info.get('fps_num')}/{info.get('fps_den')}"
    print(f"  video  {info.get('v_codec')}  {info.get('width')}x{info.get('height')}  "
          f"{fps} fps  {info.get('pix_fmt')}")
    print(f"  audio  {info.get('a_codec')}  {info.get('sample_rate')} Hz  {info.get('channels')} ch")
    print(f"  length {info.get('duration_s')} s")
    # The cheap verdict comes off the rate fields; is_vfr() re-probes packet
    # durations, and intake is exactly where the expensive answer is worth it.
    deep = is_vfr(path)
    print(f"  vfr    {deep} (rate-only guess: {info.get('is_vfr')})")
    if deep:
        print("  -> VFR. ffprobe and other tools disagree about the frame count of a VFR file;")
        print("     intake transcodes it to CFR, which is what makes src_*_frames unambiguous.")
    return 0


# --- ve transcript ------------------------------------------------------------


def cmd_transcript_flatten(args: argparse.Namespace) -> int:
    """The transcript as numbered, timestamped Markdown for the story pass.

    The story pass is the one stage the CLI cannot do, and its input is prose
    with word indices attached -- word-level JSON is not something to reason
    over. Without this verb `.claude/agents/transcript-analyst.md` had no way to
    read anything.
    """
    proj = _load(args)
    transcript = proj.get("transcript")
    if not transcript:
        raise CliError(f"no transcript; run `{PROG} transcribe {args.slug}` first")
    transcript_mod = _import("transcript", "ve transcript flatten")
    words = _entry(transcript_mod, ("load_words",), "ve transcript flatten")(
        proj.resolve(transcript["path"])
    )
    if args.plain:
        print(_entry(transcript_mod, ("flatten_text",), "ve transcript flatten")(words))
        return 0
    render = _entry(transcript_mod, ("words_to_markdown",), "ve transcript flatten")
    story = proj.get("story") or {}
    text = _call_flexible(
        render,
        words,
        title=str(proj.get("project_id")),
        language=str(transcript.get("language", "de")),
        blocks=(story.get("blocks") or None),
    )
    print(text)
    return 0


# --- ve cut -------------------------------------------------------------------


# presets/cut-profiles.json is a nested, documented document; cut.py's config is
# flat. Nothing translated between them, so `cut_profile: "balanced"` in every
# format preset was a dead key -- the CLI printed "cut.py defines no such
# profile; ignored" and every project silently ran cut.py's own defaults,
# including `aggressive`. This table IS the translation, written out field by
# field so an unmapped key is visible rather than silently dropped.
_CUT_PROFILE_MAP: tuple[tuple[str, str], ...] = (
    ("silence.min_gap_s", "min_gap"),
    ("silence.min_removal_s", "min_removal"),
    ("silence.keep_pause_floor_s", "keep_pause"),
    ("silence.pad_in_s", "pad_in"),
    ("silence.pad_out_s", "pad_out"),
    ("silence.max_cut_density_s", "max_cut_density"),
    ("silence.min_clip_s", "min_clip"),
    ("fillers.signals.dur_per_char_s", "filler.dur_per_char"),
    ("fillers.signals.flank_gap_s", "filler.flank_gap"),
    ("fillers.signals.energy_within_db", "filler.energy_tolerance_db"),
    ("fillers.tier_b.guards.min_flank_gap_ms", "filler.tier_b_gap"),   # ms -> s below
    ("fillers.tier_b.guards.max_removals_per_output_s", "filler.tier_b_min_spacing_s"),
    ("fillers.density_guard.window_s", "filler.density_window_s"),
    ("alignment_confidence.low_score", "low_score"),
    ("alignment_confidence.widen_pad_ms", "low_score_pad_extra"),      # ms -> s below
    ("retakes.interruption_gap_s", "retake.cue_gap"),
    ("retakes.left_boundary_gap_s", "retake.boundary_gap"),
    ("retakes.ngram_max", "retake.n_max"),
    ("retakes.ngram_min", "retake.n_min"),
    ("retakes.similarity_propose", "retake.sim_propose"),
    ("retakes.similarity_auto", "retake.sim_auto"),
    ("retakes.sentence_cosine", "retake.multitake_cosine"),
    ("retakes.sentence_window_s", "retake.multitake_window_s"),
    ("splice.crossfade_ms", "crossfade_ms"),
    ("splice.crossfade_min_ms", "crossfade_min_ms"),
    ("splice.crossfade_max_ms", "crossfade_max_ms"),
)
_CUT_PROFILE_MS_TO_S = {"filler.tier_b_gap", "low_score_pad_extra"}


def _dig(doc: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def _plant(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _load_cut_profile(root: str, name: str) -> tuple[dict[str, Any], list[str]]:
    """presets/cut-profiles.json -> a partial config in cut.py's flat shape."""
    notes: list[str] = []
    path = None
    for directory in (os.path.join(root, "presets"), os.path.join(repo_root(), "presets")):
        candidate = os.path.join(directory, "cut-profiles.json")
        if os.path.isfile(candidate):
            path = candidate
            break
    if path is None:
        return {}, [f"cut profile {name!r} requested but presets/cut-profiles.json is missing; using cut defaults"]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        return {}, [f"presets/cut-profiles.json unreadable ({exc}); using cut defaults"]

    profile = (doc.get("profiles") or {}).get(name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted((doc.get("profiles") or {}).keys())) or "none"
        return {}, [f"cut profile {name!r} is not in presets/cut-profiles.json (have: {available}); using cut defaults"]

    cfg: dict[str, Any] = {}
    mapped = 0
    for source, target in _CUT_PROFILE_MAP:
        value = _dig(profile, source)
        if value is None:
            continue
        if target in _CUT_PROFILE_MS_TO_S:
            value = float(value) / 1000.0
        _plant(cfg, target, value)
        mapped += 1

    tiers = _dig(profile, "silence.pause_tiers")
    if isinstance(tiers, list) and tiers:
        # cut.py wants [lo, hi, retained]; the preset states an upper bound per
        # row and leaves the lower one implicit.
        rows: list[list[float | None]] = []
        lo = 0.0
        for tier in tiers:
            hi = tier.get("gap_below_s")
            retained = tier.get("retained_s")
            if retained is not None:
                rows.append([lo, hi, retained])
            lo = hi if hi is not None else lo
        if rows:
            cfg["pause_tiers"] = rows
            mapped += 1

    # cut-profiles.json's top-level `vocabulary` block is deliberately NOT
    # forwarded. fillers.py owns the word lists in code, with per-language
    # ambiguity flags and tag-question positions the JSON has no way to express,
    # and the two have drifted: the JSON is missing 22 entries that the code
    # carries. Passing it through would silently narrow filler detection. Until
    # one of the two is deleted, the code is the one that runs.

    notes.append(f"cut profile {name!r} from presets/cut-profiles.json ({mapped} field(s) mapped)")
    if profile.get("calibrated") is False:
        notes.append(f"profile {name!r} is calibrated=false -- thresholds are research-derived, not measured")
    return cfg, notes


def _cut_config(proj: Project, root: str, cut_mod: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Preset overrides on top of cut.py's defaults. Returns (cfg, notes).

    Returns ``None`` when the preset overrides nothing, so ``build_segments``
    uses its own defaults rather than a copy of them made here.
    """
    notes: list[str] = []
    preset_name = proj.get("format_preset")
    if not preset_name:
        return None, notes
    try:
        preset = _load_format_preset(root, str(preset_name))
    except CliError as exc:
        notes.append(f"format preset unreadable ({exc}); using cut defaults")
        return None, notes

    cfg: dict[str, Any] = {}
    # The project's own cut_profile wins over the preset's: `ve cut` may have
    # been re-run after someone changed it in project.json.
    profile = proj.get("cut_profile") or preset.get("cut_profile")
    if profile:
        # A cut.py that grows its own PROFILES table takes precedence -- it would
        # be the authoritative shape, not a translation of one.
        profiles = getattr(cut_mod, "PROFILES", None)
        if isinstance(profiles, dict) and profile in profiles:
            cfg.update(profiles[profile])
            notes.append(f"cut profile {profile!r} (from cut.PROFILES)")
        else:
            mapped, profile_notes = _load_cut_profile(root, str(profile))
            cfg.update(mapped)
            notes.extend(profile_notes)
    overrides = preset.get("cut")
    if isinstance(overrides, dict):
        cfg.update(overrides)
        notes.append(f"{len(overrides)} threshold override(s) from presets/formats/{preset_name}.json")
    return (cfg or None), notes


def cmd_cut(args: argparse.Namespace) -> int:
    proj = _load(args)
    root = _root(args)
    transcript = proj.get("transcript")
    if not transcript:
        raise CliError(f"no transcript; run `{PROG} transcribe {args.slug}` first")

    transcript_mod = _import("transcript", "ve cut")
    cut_mod = _import("cut", "ve cut")
    load_words = _entry(transcript_mod, ("load_words",), "ve cut")
    build = _entry(cut_mod, ("build_segments",), "ve cut")

    words = load_words(proj.resolve(transcript["path"]))
    tb = proj.timebase()
    cfg, notes = _cut_config(proj, root, cut_mod)
    for note in notes:
        print(f"  {note}")
    story = proj.get("story") or None
    segments = _call_flexible(build, words, tb, cfg, story=story)
    if not isinstance(segments, list):
        raise CliError(f"cut.build_segments returned {type(segments).__name__}, expected a list of segment dicts")

    enabled = [s for s in segments if s.get("enabled", True)]
    frames = sum(int(s["src_out_frames"]) - int(s["src_in_frames"]) for s in enabled)
    by_reason = _disabled_by_reason(segments)

    print(f"{len(segments)} segments, {len(enabled)} enabled, {len(segments) - len(enabled)} disabled")
    for reason, count in by_reason:
        print(f"    {reason:<28} {count:>5}")
    print(f"  timeline {frames} frames = {_timecode(frames, tb)}")
    _print_review(_pending_review(segments))

    if args.dry_run:
        print("dry run: project.json not written")
        return 0

    previous = {str(s.get("id")): s for s in proj.get("segments") or []}
    locked = [sid for sid, seg in previous.items() if seg.get("locked")]
    for seg in segments:
        old = previous.get(str(seg.get("id")))
        if old is not None and old.get("locked"):
            # A locked segment is a human decision. The mechanical pass may
            # propose, it may not overwrite.
            seg.update({k: v for k, v in old.items() if k != "text"})
    if locked:
        print(f"  kept {len(locked)} locked segment(s) untouched")

    proj.data["segments"] = segments
    proj.set_stage("rough_cut")
    proj.save()
    print(f"written. next: {PROG} render {args.slug}")
    return 0


def _disabled_by_reason(segments: Iterable[dict]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for seg in segments:
        if seg.get("enabled", True):
            continue
        prov = seg.get("provenance") or {}
        reason = seg.get("reason") or prov.get("rule") or prov.get("stage") or "unspecified"
        counts[str(reason)] = counts.get(str(reason), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _pending_review(segments: Iterable[dict]) -> list[dict]:
    """Decisions the cut deliberately did NOT take, flattened for display.

    ``cut`` blocks a removal whenever a guard fires -- a sentence-initial filler
    is a topic-shift cue, a tier-C particle carries attitude, an ambiguous token
    like "um" is usually a real preposition. Those become flags on the kept
    segment instead of a cut. They were invisible until this existed, and an
    invisible proposal is one nobody ever decides.
    """
    out: list[dict] = []
    for seg in segments:
        for flag in ((seg.get("provenance") or {}).get("flags") or ()):
            out.append({
                "segment_id": seg.get("id"),
                "action": flag.get("action", "flag"),
                "kind": flag.get("kind", "?"),
                "tier": flag.get("tier") or "",
                "text": flag.get("text") or "",
                "word_range": flag.get("word_range"),
                "blocks": list(flag.get("blocks") or ()),
                "confidence": flag.get("confidence"),
            })
    # Proposals first: they are the ones awaiting a yes/no.
    out.sort(key=lambda f: (f["action"] != "propose", str(f["segment_id"])))
    return out


def _print_review(items: list[dict], *, limit: int = 8, indent: str = "  ") -> None:
    if not items:
        return
    proposals = sum(1 for i in items if i["action"] == "propose")
    flagged = len(items) - proposals
    parts = []
    if proposals:
        parts.append(f"{proposals} proposal{'s' if proposals != 1 else ''} awaiting a decision")
    if flagged:
        parts.append(f"{flagged} flagged for information")
    print(f"{indent}review     {', '.join(parts)}")
    for item in items[:limit]:
        why = ", ".join(item["blocks"]) if item["blocks"] else "no guard fired"
        tier = f"{item['kind']} {item['tier']}".strip()
        text = f"{item['text']!r}" if item["text"] else ""
        print(f"{indent}  {item['action']:<8} {tier:<10} {text:<14} {why:<24} {item['segment_id']}")
    if len(items) > limit:
        print(f"{indent}  ... and {len(items) - limit} more")


# --- ve render ----------------------------------------------------------------


def cmd_render(args: argparse.Namespace) -> int:
    proj = _load(args)
    if not proj.enabled_segments():
        raise CliError(f"no enabled segments; run `{PROG} cut {args.slug}` first")
    render_mod = _import("render", "ve render")
    render_all = _entry(render_mod, ("render_all",), "ve render")
    if args.draft and "draft" not in inspect.signature(render_all).parameters:
        # Silently dropping a quality flag would let someone ship a draft
        # encode believing it was a master.
        print("  note: this render module has no draft mode; rendering at full quality")
    out = _call_flexible(render_all, proj, only=args.only, draft=args.draft)
    # Order matters: graphics sit under the captions, which are the top layer.
    out = _composite_overlays(proj, out)
    out = _burn_captions(proj, out)
    proj.save()
    print(f"rendered -> {out}")
    print(f"  next: {PROG} qc {args.slug}")
    return 0


def _burn_captions(proj: Project, master: str) -> str:
    """Burn the ASS sidecar into the finished master, in place.

    render_all() owns picture and sound and nothing else, so without this step a
    project with ``captions.enabled`` and ``burn_in`` true renders and exports a
    master with no captions in it and no warning anywhere -- the sidecar exists,
    the QC font check passes on the sidecar, and the delivered file is bare.
    This is the seam that closes that; it costs a second H.264 encode of the
    master and should be folded into the mux pass when the overlay compositor
    lands, since that one re-encodes too.
    """
    caps = proj.get("captions") or {}
    if not (caps.get("enabled") and caps.get("burn_in")):
        return master
    ass_path = proj.resolve(str(caps.get("sidecar_path") or "out/captions.ass"))
    if not os.path.isfile(ass_path):
        print(f"  warning: captions are enabled but {ass_path} does not exist;"
              f" run `{PROG} captions {proj.get('project_id')}` -- master left bare", file=sys.stderr)
        return master
    captions_mod = _import("captions", "ve render")
    burn = _entry(captions_mod, ("burn_in",), "ve render")
    fonts_dir = _entry(captions_mod, ("fonts_dir",), "ve render")
    tmp = master + ".captioned.mp4"
    # Audio is stream-copied: the AAC in the master is already the one mux the
    # architecture allows, and re-encoding it here would be the second one.
    burn(master, ass_path, tmp, fonts=fonts_dir())
    os.replace(tmp, master)
    print(f"  burned {os.path.basename(ass_path)} into the master")
    return master


# --- ve captions --------------------------------------------------------------


def _load_caption_preset(root: str, name: str) -> dict[str, Any]:
    for directory in _preset_dirs(root, "captions"):
        path = os.path.join(directory, f"{name}.json")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    available = _disk_presets(root, "captions")
    raise CliError(
        f"no caption preset {name!r} in " + " or ".join(_preset_dirs(root, "captions"))
        + (f"; known: {', '.join(available)}" if available else "")
    )


def cmd_captions(args: argparse.Namespace) -> int:
    proj = _load(args)
    root = _root(args)
    caps = proj.data.setdefault("captions", {})
    if args.preset:
        caps["preset"] = args.preset
    caps["enabled"] = not args.disable
    if args.burn_in is not None:
        caps["burn_in"] = args.burn_in
    caps.setdefault("sidecar_path", "out/captions.ass")
    caps.setdefault("overrides", [])

    if args.disable:
        proj.save()
        print("captions disabled")
        return 0

    transcript = proj.get("transcript")
    if not transcript:
        raise CliError(f"no transcript; run `{PROG} transcribe {args.slug}` first")

    preset = _load_caption_preset(root, str(caps.get("preset")))
    transcript_mod = _import("transcript", "ve captions")
    captions_mod = _import("captions", "ve captions")
    load_words = _entry(transcript_mod, ("load_words",), "ve captions")
    build_ass = _entry(captions_mod, ("build_ass",), "ve captions")

    words = load_words(proj.resolve(transcript["path"]))
    ass = build_ass(words, proj, preset)
    if not isinstance(ass, str):
        raise CliError(f"captions.build_ass returned {type(ass).__name__}, expected the ASS document as a string")

    out_path = proj.resolve(caps["sidecar_path"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # UTF-8 without BOM: libass reads the BOM as part of the first Script Info
    # key and the whole header falls apart silently.
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ass)
    proj.set_stage("captions")
    proj.save()

    events = sum(1 for line in ass.splitlines() if line.startswith("Dialogue:"))
    print(f"{events} caption events -> {out_path}")
    result = run_qc(proj, checks=["font_fallback"])
    print(format_findings(result))
    return 0


# --- ve music -----------------------------------------------------------------


def cmd_music(args: argparse.Namespace) -> int:
    proj = _load(args)
    path = os.path.abspath(args.path)
    if not os.path.isfile(path):
        raise CliError(f"no such file: {path}")
    info = probe(path)
    if not info.get("has_audio"):
        raise CliError(f"{path} has no audio stream")

    tracks = proj.data.setdefault("audio_tracks", [])
    if not any(t.get("id") == "vo" for t in tracks):
        tracks.insert(0, {"id": "vo", "kind": "dialogue", "source": "segments", "gain_db": 0.0})

    track = {
        "id": args.id,
        "kind": "music",
        "path": _rel_to_project(proj, path),
        "timeline_in_frames": int(args.at),
        "gain_db": float(args.db),
        "fade_in_s": float(args.fade_in),
        "fade_out_s": float(args.fade_out),
        "ducking": {
            "enabled": bool(args.duck),
            "sidechain": "vo",
            # sidechaincompress defaults from the contract's schema example.
            # Research-derived: no ear has checked them against a real mix.
            "threshold": 0.03,
            "ratio": 8,
            "attack_ms": 20,
            "release_ms": 400,
        },
    }
    for i, existing in enumerate(tracks):
        if existing.get("id") == args.id:
            tracks[i] = track
            break
    else:
        tracks.append(track)

    proj.set_stage("music")
    proj.save()
    dur = info.get("duration_s") or 0.0
    print(f"music track {args.id!r}: {track['path']}")
    print(
        f"  {dur:.1f} s  {info.get('sample_rate')} Hz  {info.get('channels')} ch  "
        f"gain {track['gain_db']:+.1f} dB  ducking {'on' if args.duck else 'off'}"
    )
    print(f"  next: {PROG} render {args.slug}")
    return 0


# --- ve export ----------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    proj = _load(args)
    out_cfg = proj.get("output") or {}
    master = proj.resolve(out_cfg.get("path") or "out/final.mp4")
    if not os.path.isfile(master):
        raise CliError(f"no master render at {master}; run `{PROG} render {args.slug}` first")

    result = run_qc(proj)
    print(format_findings(result))
    if result["exit_code"] == 2 and not args.force:
        print("\nexport refused: QC found failures. Fix them, or pass --force to ship anyway.", file=sys.stderr)
        return 2

    stamp = _dt.datetime.now().strftime("%Y%m%d")
    platform = out_cfg.get("platform", "youtube")
    ext = os.path.splitext(master)[1] or ".mp4"
    name = f"{proj.get('project_id')}_{platform}_{stamp}{ext}"
    if not args.to:
        dst = os.path.join(proj.dir, "out", name)
    else:
        dst = os.path.abspath(args.to)
        # A --to with no file extension is a directory the user has not created
        # yet. Treating it as a filename silently produces an mp4 called
        # "delivery" with no extension that no player will open by double-click.
        if os.path.isdir(dst) or not os.path.splitext(dst)[1]:
            os.makedirs(dst, exist_ok=True)
            dst = os.path.join(dst, name)
    if os.path.abspath(dst) != os.path.abspath(master):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(master, dst)

    info = probe(dst)
    tb = proj.timebase()
    manifest = {
        "project_id": proj.get("project_id"),
        "exported_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platform": platform,
        "file": os.path.basename(dst),
        "bytes": os.path.getsize(dst),
        "timeline_frames": proj.total_frames,
        "timeline_seconds": round(tb.frames_to_seconds(proj.total_frames), 3),
        "enabled_segments": len(proj.enabled_segments()),
        "resolution": [info.get("width"), info.get("height")],
        "v_codec": info.get("v_codec"),
        "a_codec": info.get("a_codec"),
        "loudnorm_target": out_cfg.get("loudnorm"),
        "qc": {"ok": result["ok"], "counts": result["counts"]},
    }
    manifest_path = os.path.splitext(dst)[0] + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    proj.set_stage("export")
    proj.save()
    print(f"\nexported {dst} ({_human_bytes(manifest['bytes'])})")
    print(f"  manifest {manifest_path}")
    if args.open:
        _reveal(dst)
    return 0


def _reveal(path: str) -> None:
    """Open the containing folder with the file selected. Best effort."""
    try:
        subprocess.run(["explorer.exe", "/select,", os.path.normpath(path)], check=False, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"  (could not open explorer: {exc})", file=sys.stderr)


# --- ve qc --------------------------------------------------------------------


def cmd_qc(args: argparse.Namespace) -> int:
    proj = _load(args)
    result = run_qc(proj, checks=args.check or None)
    if args.json:
        _emit(result)
    else:
        print(format_findings(result))
    return int(result["exit_code"])


# --- ve status ----------------------------------------------------------------


def _status_data(proj: Project) -> dict[str, Any]:
    tb = proj.timebase()
    out_cfg = proj.get("output") or {}
    segments = proj.get("segments") or []
    enabled = proj.enabled_segments()

    warm = 0
    for seg in enabled:
        if segment_cache_path(proj, seg):
            warm += 1

    sources = []
    total_source_frames = 0
    for src in proj.get("sources") or []:
        p = src.get("probe") or {}
        frames = int(p.get("duration_frames") or 0)
        total_source_frames += frames
        sources.append(
            {
                "id": src.get("id"),
                "role": src.get("role"),
                "path": src.get("path"),
                "frames": frames,
                "timecode": _timecode(frames, tb),
                "v_codec": p.get("v_codec"),
                "a_codec": p.get("a_codec"),
                "resolution": [p.get("width"), p.get("height")],
            }
        )

    story = proj.get("story") or {}
    blocks = story.get("blocks") or []
    block_counts: dict[str, int] = {}
    for blk in blocks:
        d = str(blk.get("decision", "maybe"))
        block_counts[d] = block_counts.get(d, 0) + 1

    master_rel = out_cfg.get("path") or "out/final.mp4"
    master = proj.resolve(master_rel)
    has_master = os.path.isfile(master) and os.path.getsize(master) > 0
    # A timeline edit after the render leaves a master that is real, playable,
    # and wrong. Nothing else in the pipeline notices, so `ve status` has to.
    #
    # Compared against _computed.generated_utc rather than project.json's own
    # mtime: recompute() only refreshes that stamp when a DERIVED value actually
    # changed, so `ve export` writing a new stage does not mark its own master
    # stale, while `ve cut` moving one boundary does.
    master_stale = False
    generated = (proj.get("_computed") or {}).get("generated_utc")
    if has_master and generated:
        try:
            recomputed_at = calendar.timegm(time.strptime(generated, "%Y-%m-%dT%H:%M:%SZ"))
            master_stale = recomputed_at > os.path.getmtime(master) + 1.0
        except (ValueError, OSError):
            master_stale = False

    try:
        cache_stats = Cache().stats()
    except Exception:  # noqa: BLE001 - a broken cache must not break `ve status`
        cache_stats = {"files": 0, "bytes": 0, "gb": 0.0}

    return {
        "project_id": proj.get("project_id"),
        "path": proj.path,
        "stage": proj.stage,
        "format_preset": proj.get("format_preset"),
        "platform": out_cfg.get("platform"),
        "resolution": out_cfg.get("resolution"),
        "timebase": tb.to_dict(),
        "calibrated": bool(proj.get("calibrated")),
        "sources": sources,
        "source_frames_total": total_source_frames,
        "transcript": proj.get("transcript"),
        "story_blocks": {"total": len(blocks), "by_decision": block_counts, "hook": bool(story.get("hook"))},
        "segments": {
            "total": len(segments),
            "enabled": len(enabled),
            "disabled": len(segments) - len(enabled),
            "locked": sum(1 for s in segments if s.get("locked")),
            "disabled_by_reason": _disabled_by_reason(segments),
            "review": _pending_review(segments),
        },
        "timeline": {
            "frames": proj.total_frames,
            "seconds": round(tb.frames_to_seconds(proj.total_frames), 3),
            "timecode": _timecode(proj.total_frames, tb),
        },
        "cache": {
            "warm_segments": warm,
            "cold_segments": len(enabled) - warm,
            "warm_pct": round(100.0 * warm / len(enabled), 1) if enabled else 0.0,
            "files": cache_stats["files"],
            "bytes": cache_stats["bytes"],
        },
        "overlays": len(proj.get("overlays") or []),
        "captions": dict(proj.get("captions") or {}),
        "audio_tracks": [t.get("id") for t in proj.get("audio_tracks") or []],
        "master": {
            "path": master_rel,
            "exists": has_master,
            "stale": master_stale,
            "bytes": os.path.getsize(master) if has_master else 0,
        },
        "next": _next_action(proj, has_master, master_stale),
    }


def _next_action(proj: Project, has_master: bool, master_stale: bool = False) -> dict[str, str]:
    """What to do next, derived from what is actually on disk.

    Derived rather than read from ``stage`` on purpose: ``stage`` is a label a
    human can set wrong, while "there are no segments" is a fact.
    """
    slug = str(proj.get("project_id"))
    if has_master and not master_stale:
        if proj.stage == "export":
            return {"command": "-", "why": "exported; the master is newer than the last edit"}
        return {"command": f"{PROG} qc {slug} && {PROG} export {slug}", "why": "master is current but not exported"}
    if has_master and master_stale:
        return {"command": f"{PROG} render {slug}", "why": "project.json changed after the master was rendered"}
    if not (proj.get("sources") or []):
        return {"command": f"{PROG} intake {slug} <media...>", "why": "no sources registered"}
    if not proj.get("transcript"):
        return {"command": f"{PROG} transcribe {slug}", "why": "no transcript"}
    if not ((proj.get("story") or {}).get("blocks")):
        return {
            "command": f"(story pass) have Claude read the transcript and write story.blocks",
            "why": "content selection has not happened; the mechanical cut would just tighten the ramble",
        }
    if not (proj.get("segments") or []):
        return {"command": f"{PROG} cut {slug}", "why": "no segments"}
    return {"command": f"{PROG} render {slug}", "why": "no master render"}


def cmd_status(args: argparse.Namespace) -> int:
    proj = _load(args)
    data = _status_data(proj)
    if args.json:
        _emit(data)
        return 0

    tb = data["timebase"]
    res = data["resolution"] or [0, 0]
    print(
        f"{data['project_id']}   stage: {data['stage']}"
        f"   {data['format_preset']} -> {data['platform']}"
        f"   {res[0]}x{res[1]} @ {tb['fps_num']}/{tb['fps_den']}  {tb['sample_rate']} Hz"
    )
    print(f"  {data['path']}")
    if not data["calibrated"]:
        print("  calibrated: no -- cut thresholds are research defaults, not measured")
    print()

    src = data["sources"]
    if src:
        print(f"sources      {len(src)} registered, {_timecode(data['source_frames_total'], proj.timebase())} of material")
        for s in src:
            print(
                f"  {s['id']:<3} {str(s['role']):<7} {str(s['path']):<34} "
                f"{s['frames']:>8} f  {s['timecode']:>9}  {s['v_codec']}/{s['a_codec']}"
            )
    else:
        print("sources      none")

    tr = data["transcript"]
    if tr:
        print(
            f"transcript   {tr.get('word_count')} words  {tr.get('language')}  "
            f"{tr.get('engine')} / {tr.get('align_model') or 'no align model'}  "
            f"[{tr.get('timestamp_source')}]"
        )
    else:
        print("transcript   none")

    sb = data["story_blocks"]
    if sb["total"]:
        by = ", ".join(f"{v} {k}" for k, v in sorted(sb["by_decision"].items()))
        print(f"story        {sb['total']} blocks: {by}" + ("  (hook set)" if sb["hook"] else ""))
    else:
        print("story        no content-selection pass yet")

    seg = data["segments"]
    print(f"segments     {seg['total']} total, {seg['enabled']} enabled, {seg['disabled']} disabled, {seg['locked']} locked")
    for reason, count in seg["disabled_by_reason"]:
        print(f"  disabled   {reason:<30} {count:>5}")
    _print_review(seg["review"], indent="")

    tl = data["timeline"]
    print(f"timeline     {tl['frames']} frames = {tl['timecode']} ({tl['seconds']:.3f} s)")

    ca = data["cache"]
    print(
        f"cache        {ca['warm_segments']}/{seg['enabled']} enabled segments warm ({ca['warm_pct']}%)"
        f"   store: {ca['files']} files, {_human_bytes(ca['bytes'])}"
    )

    caps = data["captions"]
    print(
        f"captions     {'on' if caps.get('enabled') else 'off'}"
        f"  preset {caps.get('preset')}  {'burn-in' if caps.get('burn_in') else 'sidecar only'}"
        f"  overrides {len(caps.get('overrides') or [])}"
    )
    print(f"overlays     {data['overlays']}   audio tracks: {', '.join(data['audio_tracks']) or 'none'}")
    m = data["master"]
    if not m["exists"]:
        print(f"master       {m['path']} -- not rendered")
    else:
        print(
            f"master       {m['path']} {_human_bytes(m['bytes'])}"
            + ("   STALE: project.json changed after this was rendered" if m["stale"] else "")
        )
    print()
    print(f"next         {data['next']['command']}")
    print(f"             ({data['next']['why']})")
    return 0


# --- ve gfx -------------------------------------------------------------------


def cmd_gfx(args: argparse.Namespace) -> int:
    """Render every overlay in the project to a cached alpha .mov.

    The graphics lane existed as a module with nothing calling it, so a project
    could carry ``overlays[]`` forever and never get a pixel. This is the seam.
    Compositing happens in `ve render`, not here, because the overlay has to
    land on the finished master at its DERIVED timeline position.
    """
    proj = _load(args)
    overlays = proj.get("overlays") or []
    if not overlays:
        print("no overlays in this project; nothing to render")
        return 0
    gfx = _import("gfx", "ve gfx")
    render_overlay = _entry(gfx, ("render_overlay",), "ve gfx")
    tb = proj.timebase()
    res = (proj.get("output") or {}).get("resolution") or [1920, 1080]
    size = (int(res[0]), int(res[1]))
    cache = Cache()

    for ov in overlays:
        oid = str(ov.get("id"))
        frames = int(ov.get("duration_frames") or 0)
        if frames <= 0:
            raise CliError(f"overlay {oid} has duration_frames={frames}; nothing to render")
        record = _call_flexible(
            render_overlay, ov["template"], ov.get("props") or {}, frames, tb, cache, size=size,
            workers=args.workers,
        )
        # Store the path project-relative: the cache is global and the project
        # must stay movable.
        ov["render"] = {**record, "path": _rel_to_project(proj, record["path"])}
        print(f"  {oid:<10} {frames:>5} frames  {record['cache_key'][:12]}  {record['path']}")

    proj.set_stage("graphics")
    proj.save()
    result = run_qc(proj, checks=["alpha", "chrome_blank"])
    print(format_findings(result))
    print(f"  next: {PROG} render {args.slug}")
    return 2 if result["exit_code"] == 2 else 0


def _composite_overlays(proj: Project, master: str) -> str:
    """Burn rendered overlays onto the finished master, in place."""
    overlays = [ov for ov in (proj.get("overlays") or []) if (ov.get("render") or {}).get("path")]
    if not overlays:
        return master
    gfx = _import("gfx", "ve render")
    composite = _entry(gfx, ("composite_overlays",), "ve render")
    to_timeline = _entry(gfx, ("overlays_from_project",), "ve render")
    items = to_timeline(proj)
    if not items:
        return master
    tmp = master + ".ovl.mp4"
    composite(master, items, tmp, proj.timebase())
    os.replace(tmp, master)
    print(f"  composited {len(items)} overlay(s) onto the master")
    return master


# --- ve cache gc --------------------------------------------------------------


def _scene_dir(root: str, slug: str) -> str:
    return os.path.join(_project_dir(root, slug), "scene")


def _find_asr_model(root: str) -> str | None:
    """The ggml model the ffmpeg whisper filter needs, if one was fetched."""
    directory = os.path.join(root, ".cache", "models")
    if not os.path.isdir(directory):
        return None
    names = [n for n in sorted(os.listdir(directory))
             if n.startswith("ggml-") and n.endswith(".bin")]
    # largest wins: large-v3 beats base, and base is not good enough for German
    if not names:
        return None
    return max((os.path.join(directory, n) for n in names), key=os.path.getsize)


def cmd_scene_new(args: argparse.Namespace) -> int:
    from . import scene as scenelib

    root = _root(args)
    pdir = _project_dir(root, args.slug)
    sdir, mdir = _scene_dir(root, args.slug), os.path.join(pdir, "media")
    for d in (sdir, mdir, os.path.join(pdir, "out")):
        os.makedirs(d, exist_ok=True)

    if not os.path.isfile(args.audio):
        raise CliError(f"no such audio file: {args.audio}")
    dest = os.path.join(mdir, "vo" + os.path.splitext(args.audio)[1].lower())
    shutil.copy2(args.audio, dest)

    model = _find_asr_model(root)
    if model is None:
        raise CliError(
            "no ggml model in .cache/models. Fetch one first, e.g.\n"
            "  curl -L -o .cache/models/ggml-large-v3-turbo.bin \\\n"
            "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin")

    info = probe(dest)
    duration = float(info.get("duration_s") or 0.0)
    print(f"transcribing {os.path.basename(dest)} ({duration:.2f}s) with {os.path.basename(model)} ...")
    words, sents = scenelib.transcribe_audio(dest, model, sdir, language=args.language)
    with open(os.path.join(sdir, "words.json"), "w", encoding="utf-8") as fh:
        json.dump({"words": words, "sents": sents, "duration": duration},
                  fh, ensure_ascii=False, indent=1)

    template = os.path.join(root, "presets", "scene", "build_scene.py")
    target = os.path.join(sdir, "build_scene.py")
    if os.path.isfile(template) and not os.path.isfile(target):
        shutil.copy2(template, target)

    print(f"  {len(words)} words, {len(sents)} sentences -> {os.path.join(sdir, 'words.json')}")
    print(f"  scene source: {target}")
    print("\nnext: edit the beat table in build_scene.py, then")
    print(f"  ve scene render {args.slug}")
    return 0


def cmd_scene_render(args: argparse.Namespace) -> int:
    from . import scene as scenelib

    root = _root(args)
    sdir = _scene_dir(root, args.slug)
    builder = os.path.join(sdir, "build_scene.py")
    html = os.path.join(sdir, "scene.html")

    if os.path.isfile(builder):
        r = subprocess.run([sys.executable, builder], cwd=sdir, check=False)
        if r.returncode != 0:
            raise CliError(f"build_scene.py failed (exit {r.returncode})")
    if not os.path.isfile(html):
        raise CliError(f"no scene.html at {html}; run `ve scene new {args.slug} <audio>` first")

    duration = args.duration
    if duration is None:
        with open(os.path.join(sdir, "words.json"), "r", encoding="utf-8") as fh:
            duration = float(json.load(fh).get("duration") or 0.0)
        duration += 0.4  # let the closing beat come to rest before the audio ends
    if duration <= 0:
        raise CliError("could not determine duration; pass --duration")

    frames = scenelib.render_frames(
        html, os.path.join(root, ".cache"), fps=args.fps, duration=duration,
        width=args.width, height=args.height, workers=args.workers)
    out = os.path.join(_project_dir(root, args.slug), "out", "scene_silent.mp4")
    scenelib.encode_frames(frames, out, fps=args.fps)
    print(f"-> {out}")
    print(f"next: ve scene finish {args.slug}")
    return 0


def cmd_scene_finish(args: argparse.Namespace) -> int:
    from . import scene as scenelib

    root = _root(args)
    pdir = _project_dir(root, args.slug)
    silent = os.path.join(pdir, "out", "scene_silent.mp4")
    if not os.path.isfile(silent):
        raise CliError(f"no rendered picture at {silent}; run `ve scene render {args.slug}` first")

    mdir = os.path.join(pdir, "media")
    voices = [os.path.join(mdir, n) for n in sorted(os.listdir(mdir))
              if n.lower().startswith("vo.")] if os.path.isdir(mdir) else []
    if not voices:
        raise CliError(f"no voice-over (media/vo.*) in {mdir}")

    out = os.path.join(pdir, "out", f"{args.slug}.mp4")
    scenelib.finish(silent, voices[0], out, headroom_db=args.headroom)
    loud = scenelib.measure_loudness(out)
    info = probe(out)
    print(out)
    print(f"  {info.get('width')}x{info.get('height')} @ {info.get('fps_num')}/{info.get('fps_den')}"
          f"  {float(info.get('duration_s') or 0):.2f}s")
    if loud:
        print(f"  I {loud.get('I')} LUFS   LRA {loud.get('LRA')} LU   peak {loud.get('peak')} dBFS")
    return 0


def cmd_cache_gc(args: argparse.Namespace) -> int:
    root = _root(args)
    cache = Cache()
    protect: set[str] = set()
    for slug in _list_slugs(root):
        try:
            proj = Project.load(os.path.join(_project_dir(root, slug), "project.json"))
        except (ProjectError, VeditError):
            continue
        protect |= cache.protect_keys_from_project(proj)

    print(f"cache {cache.root}")
    before = cache.stats()
    print(f"  before: {before['files']} files, {_human_bytes(before['bytes'])}")
    print(f"  protecting {len(protect)} key(s) referenced by {len(_list_slugs(root))} project(s)")
    print(f"  budget {args.max_gb} GB, keeping anything used in the last {args.keep_days} day(s)")
    if args.dry_run:
        print("  dry run: nothing deleted")
        return 0

    result = cache.gc(args.max_gb, args.keep_days, protect)
    if args.json:
        _emit(result)
        return 0
    print(
        f"  deleted {result['deleted']} of {result['scanned']} artifact(s), "
        f"freed {_human_bytes(result['freed_bytes'])}, "
        f"{_human_bytes(result['kept_bytes'])} kept"
    )
    if result.get("orphan_tmp_removed"):
        print(f"  removed {result['orphan_tmp_removed']} orphaned tmp file(s) from crashed renders")
    return 0


def cmd_cache_stats(args: argparse.Namespace) -> int:
    cache = Cache()
    stats = cache.stats()
    if args.json:
        _emit({"root": cache.root, **stats})
        return 0
    print(f"cache {cache.root}")
    print(f"  {stats['files']} files, {_human_bytes(stats['bytes'])}")
    return 0


# --- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Frame-exact video editing pipeline. Integer frames on one timebase are the only authority.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical run:\n"
            f"  {PROG} new ep042 --preset longform --platform youtube\n"
            f"  {PROG} intake ep042 C:\\Videos\\cam_a.MP4\n"
            f"  {PROG} transcribe ep042 --language de\n"
            "  (story pass: Claude reads the transcript and writes story.blocks)\n"
            f"  {PROG} cut ep042 --dry-run\n"
            f"  {PROG} render ep042\n"
            f"  {PROG} qc ep042\n"
            f"  {PROG} export ep042 --open\n"
        ),
    )
    p.add_argument("--root", help="repo root (default: the Editing/ directory this engine lives in)")
    p.add_argument("--version", action="version", version=f"{PROG} " + _version())
    sub = p.add_subparsers(dest="command", metavar="<command>")

    # new
    sp = sub.add_parser("new", help="create a project")
    sp.add_argument("slug")
    sp.add_argument("--preset", default="longform", help="format preset from presets/formats (default: longform)")
    sp.add_argument("--platform", choices=PLATFORMS, help="delivery platform; drives the loudness target")
    sp.set_defaults(func=cmd_new)

    # intake
    sp = sub.add_parser("intake", help="CFR-normalize media into the project, probe it, register it")
    sp.add_argument("slug")
    sp.add_argument("media", nargs="+", help="source files; every one is transcoded to the project timebase")
    sp.add_argument("--role", default="acam", choices=("acam", "broll", "screen"), help="default: acam")
    sp.add_argument("--force", action="store_true", help="overwrite an existing normalized file")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_intake)

    # transcribe
    sp = sub.add_parser("transcribe", help="word-level transcription via the .venv-asr sidecar")
    sp.add_argument("slug")
    sp.add_argument("--language", default="de")
    sp.add_argument("--source", help="source id (default: the first acam source)")
    sp.add_argument("--model", help="ASR model name passed through to the driver")
    sp.add_argument("--force", action="store_true", help="re-transcribe even when the cache has it")
    sp.set_defaults(func=cmd_transcribe)

    # cut
    sp = sub.add_parser("cut", help="mechanical pass over story-kept blocks")
    sp.add_argument("slug")
    sp.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    sp.set_defaults(func=cmd_cut)

    # render
    sp = sub.add_parser("render", help="render segments (cached) and mux the master")
    sp.add_argument("slug")
    sp.add_argument("--only", help="render one segment id, e.g. seg_0012")
    sp.add_argument("--draft", action="store_true", help="fast, low-quality pass for looking at the edit")
    sp.set_defaults(func=cmd_render)

    # captions
    sp = sub.add_parser("captions", help="build the ASS sidecar and enable captions")
    sp.add_argument("slug")
    sp.add_argument("--preset", help="caption preset from presets/captions")
    sp.add_argument("--burn-in", dest="burn_in", action="store_true", default=None)
    sp.add_argument("--no-burn-in", dest="burn_in", action="store_false")
    sp.add_argument("--disable", action="store_true", help="turn captions off for this project")
    sp.set_defaults(func=cmd_captions)

    # music
    sp = sub.add_parser("music", help="register a music bed")
    sp.add_argument("slug")
    sp.add_argument("path")
    sp.add_argument("--db", type=float, default=-22.0, help="gain in dB (default: -22)")
    sp.add_argument("--duck", action="store_true", help="sidechain-duck under the dialogue track")
    sp.add_argument("--at", type=int, default=0, help="timeline start in frames (default: 0)")
    sp.add_argument("--fade-in", type=float, default=1.5)
    sp.add_argument("--fade-out", type=float, default=3.0)
    sp.add_argument("--id", default="music")
    sp.set_defaults(func=cmd_music)

    # export
    sp = sub.add_parser("export", help="run QC, stamp a delivery copy, write a manifest")
    sp.add_argument("slug")
    sp.add_argument("--open", action="store_true", help="reveal the file in Explorer")
    sp.add_argument("--to", help="destination file or directory")
    sp.add_argument("--force", action="store_true", help="export even when QC fails")
    sp.set_defaults(func=cmd_export)

    # qc
    sp = sub.add_parser("qc", help="the QC contract: exits 0 ok / 1 warnings / 2 failures")
    sp.add_argument("slug")
    sp.add_argument("--check", action="append", help="run only these checks (letters a-h or names); repeatable")
    sp.add_argument("--json", action="store_true", help="machine-readable findings")
    sp.set_defaults(func=cmd_qc)

    # status
    sp = sub.add_parser("status", help="where this project is and what to do next")
    sp.add_argument("slug")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    # probe
    sp = sub.add_parser("probe", help="what is in a media file, and is it VFR")
    sp.add_argument("path")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_probe)

    # transcript
    sp = sub.add_parser("transcript", help="views onto the word-level transcript")
    tsub = sp.add_subparsers(dest="transcript_command", metavar="<subcommand>")
    fl = tsub.add_parser("flatten", help="numbered, timestamped Markdown for the story pass")
    fl.add_argument("slug")
    fl.add_argument("--plain", action="store_true", help="prose only, no indices or timestamps")
    fl.add_argument("--numbered", action="store_true", default=True,
                    help="numbered (the default; accepted so the skills read literally)")
    fl.set_defaults(func=cmd_transcript_flatten)
    sp.set_defaults(func=lambda a: (sp.print_help(), 1)[1])

    # gfx
    sp = sub.add_parser("gfx", help="render the project's HTML overlays to cached alpha movies")
    sp.add_argument("slug")
    sp.add_argument("--workers", type=int, default=4,
                    help="parallel Chrome screenshots (default 4; ~450 ms per frame each)")
    sp.set_defaults(func=cmd_gfx)

    # scene -- the audio-only lane
    sp = sub.add_parser("scene", help="audio-only projects: the picture IS the graphic")
    ssub = sp.add_subparsers(dest="scene_command", metavar="<subcommand>")

    sn = ssub.add_parser("new", help="transcribe a voice-over and scaffold an editable scene")
    sn.add_argument("slug")
    sn.add_argument("audio", help="the voice-over (mp3/wav/m4a)")
    sn.add_argument("--language", default="de")
    sn.set_defaults(func=cmd_scene_new)

    sr = ssub.add_parser("render", help="build scene.html, sample it per frame, encode the picture")
    sr.add_argument("slug")
    sr.add_argument("--fps", type=int, default=30)
    sr.add_argument("--width", type=int, default=1080)
    sr.add_argument("--height", type=int, default=1920)
    sr.add_argument("--workers", type=int, default=5,
                    help="parallel Chrome screenshots; ~4.5 fps total at 5")
    sr.add_argument("--duration", type=float, default=None,
                    help="override; defaults to the voice-over length plus a tail")
    sr.set_defaults(func=cmd_scene_render)

    sf = ssub.add_parser("finish", help="mux the picture against the voice-over")
    sf.add_argument("slug")
    sf.add_argument("--headroom", type=float, default=-1.0,
                    help="dB of true-peak headroom; AAC can overshoot the source peak")
    sf.set_defaults(func=cmd_scene_finish)
    sp.set_defaults(func=lambda a: (sp.print_help(), 1)[1])

    # cache
    sp = sub.add_parser("cache", help="the shared content-addressed cache")
    csub = sp.add_subparsers(dest="cache_command", metavar="<subcommand>")
    gc = csub.add_parser("gc", help="evict by age then LRU, never touching a key an open project references")
    gc.add_argument("--max-gb", type=float, default=80.0)
    gc.add_argument("--keep-days", type=int, default=21)
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--json", action="store_true")
    gc.set_defaults(func=cmd_cache_gc)
    st = csub.add_parser("stats", help="size of the cache")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_cache_stats)
    sp.set_defaults(func=lambda a: (sp.print_help(), 1)[1])

    # projects
    sp = sub.add_parser("projects", help="list projects")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_projects)

    return p


def cmd_projects(args: argparse.Namespace) -> int:
    root = _root(args)
    slugs = _list_slugs(root)
    if args.json:
        _emit(slugs)
        return 0
    if not slugs:
        print(f"no projects in {os.path.join(root, 'projects')}; try `{PROG} new <slug>`")
        return 0
    for slug in slugs:
        try:
            proj = Project.load(os.path.join(_project_dir(root, slug), "project.json"))
            tb = proj.timebase()
            print(f"  {slug:<20} {proj.stage:<12} {len(proj.enabled_segments()):>5} seg  {_timecode(proj.total_frames, tb)}")
        except (ProjectError, VeditError) as exc:
            print(f"  {slug:<20} UNREADABLE: {exc}")
    return 0


def _version() -> str:
    from . import __version__

    return __version__


# --- entry point --------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except StageNotReady as exc:
        print(f"{PROG}: not ready: {exc}", file=sys.stderr)
        return 1
    except CliError as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except VeditError as exc:
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\n{PROG}: interrupted", file=sys.stderr)
        return 130
