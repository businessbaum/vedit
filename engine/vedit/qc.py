"""The ONE quality-control contract (CONTRACT section 9).

The QC contract was previously specified in three places and implemented in
none. This module is the single definition. Everything -- the CLI, a skill, an
agent, a hook -- calls ``run_qc()`` and reads the same shape back:

    {"ok": bool, "findings": [{severity, code, stage, message, evidence_path}]}

Exit codes are derived from the findings, never decided independently:
0 = clean, 1 = warnings only, 2 = at least one failure.

Every check that CAN produce a lookable artifact does. A frame-count mismatch
without a filmstrip is a number; with one, a human or Claude can open the PNG
and see the black tail. That is why ``evidence_path`` is part of the contract
and not a nicety.

The seven mandatory checks (contract 9 a-g):

    a  frame_count    output frame count == sum of (out - in) over enabled segments
    b  audio_samples  audio sample count == frames / fps * 48000
    c  concat_params  ffprobe parameter match across segments BEFORE any -c copy
    d  alpha          one raw rgba pixel of a graphic reads a=0
    e  font_fallback  a caption render must hash differently from an Arial control
    f  loudness       ebur128 on the master vs the platform's loudness target
    g  chrome_blank   a graphic frame must not be a uniform (blank) page

One addition beyond the mandatory seven:

    h  calibration    warns while project.calibrated is false

It is a warning, never a failure. Every threshold in presets/ is research-derived
and has not been perceptually tested against real footage; shipping a green QC
run over uncalibrated defaults would be the report lying by omission.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from typing import Any, Callable, Iterable

from .ffmpeg import FFMPEG, VeditError, escape_filter_path, run
from .probe import concat_compatible, count_audio_samples, count_frames, probe
from .project import Project

# --- the check registry -------------------------------------------------------
#
# (letter, name, pipeline stage). Both the letter and the name select a check
# through the ``checks=`` argument, so a skill can say ``checks=["a", "c"]`` and
# a human can say ``checks=["frame_count"]``.

CHECKS: tuple[tuple[str, str, str], ...] = (
    ("a", "frame_count", "render"),
    ("b", "audio_samples", "audio"),
    ("c", "concat_params", "render"),
    ("d", "alpha", "graphics"),
    ("e", "font_fallback", "captions"),
    ("f", "loudness", "master"),
    ("g", "chrome_blank", "graphics"),
    ("h", "calibration", "project"),
)

MANDATORY = ("a", "b", "c", "d", "e", "f", "g")

SEVERITIES = ("fail", "warn", "info")
_SEV_RANK = {"fail": 0, "warn": 1, "info": 2}

# Containers whose decoder pads: AAC carries 1024 or 2048 priming samples that
# no muxer removes exactly. Measured elsewhere in this repo as ~10.67 ms per
# junction; here it is a one-off at the head of the single muxed master.
_LOSSY_AUDIO_TOLERANCE_SAMPLES = 2048

# Video codecs that can actually carry alpha. Anything else fails check (d) on
# the codec alone -- a transparent-looking preview in a player that composites
# over its own background is the classic false pass.
_ALPHA_PIX_FMTS = ("rgba", "argb", "bgra", "abgr", "yuva420p", "yuva422p", "yuva444p", "rgba64", "yuva444p10")

_SEG_EXT_CANDIDATES = (".mkv", ".mp4", ".mov", ".ts")


class Finding(dict):
    """A finding is a plain dict; this subclass only exists to name the shape."""


def _f(severity: str, code: str, stage: str, message: str, evidence_path: str | None = None) -> Finding:
    if severity not in SEVERITIES:
        raise ValueError(f"bad severity {severity!r}")
    return Finding(
        severity=severity,
        code=code,
        stage=stage,
        message=message,
        evidence_path=os.path.abspath(evidence_path) if evidence_path else None,
    )


# --- public entry point -------------------------------------------------------


def run_qc(project: Project | str, checks: Iterable[str] | None = None) -> dict[str, Any]:
    """Run the QC contract over a project. Never raises for a QC failure.

    A check that cannot run (nothing rendered yet, no overlays, captions off)
    emits an ``info`` finding saying so rather than silently passing. A QC run
    that reports nothing is indistinguishable from a QC run that found nothing,
    and only one of those is good news.

    An unexpected exception inside a check becomes a ``fail`` finding with the
    exception text: a crashed check must never be reported as a passed one.
    """
    proj = project if isinstance(project, Project) else Project.load(str(project))
    selected = _select(checks)

    evidence_dir = os.path.join(proj.dir, "out", "qc")
    findings: list[Finding] = []
    ran: list[str] = []

    handlers: dict[str, Callable[[Project, str], list[Finding]]] = {
        "frame_count": _check_frame_count,
        "audio_samples": _check_audio_samples,
        "concat_params": _check_concat_params,
        "alpha": _check_alpha,
        "font_fallback": _check_font_fallback,
        "loudness": _check_loudness,
        "chrome_blank": _check_chrome_blank,
        "calibration": _check_calibration,
    }

    for letter, name, stage in CHECKS:
        if name not in selected:
            continue
        ran.append(name)
        try:
            findings.extend(handlers[name](proj, evidence_dir))
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            findings.append(
                _f("fail", f"qc.{name}.crashed", stage, f"check {letter} ({name}) raised {type(exc).__name__}: {exc}")
            )

    findings.sort(key=lambda x: (_SEV_RANK[x["severity"]], x["code"]))
    counts = {sev: sum(1 for x in findings if x["severity"] == sev) for sev in SEVERITIES}
    ok = counts["fail"] == 0

    return {
        "ok": ok,
        "findings": [dict(x) for x in findings],
        "exit_code": exit_code(findings),
        "counts": counts,
        "checks_run": ran,
        "project": proj.path,
        "project_id": proj.get("project_id"),
        "stage": proj.stage,
    }


def exit_code(findings: Iterable[dict]) -> int:
    """0 clean, 1 warnings, 2 failures. The only place this mapping exists."""
    sevs = {x.get("severity") for x in findings}
    if "fail" in sevs:
        return 2
    if "warn" in sevs:
        return 1
    return 0


def _select(checks: Iterable[str] | None) -> set[str]:
    if checks is None:
        return {name for _, name, _ in CHECKS}
    by_letter = {letter: name for letter, name, _ in CHECKS}
    names = {name for _, name, _ in CHECKS}
    out: set[str] = set()
    for raw in checks:
        token = str(raw).strip().lower()
        if token in names:
            out.add(token)
        elif token in by_letter:
            out.add(by_letter[token])
        elif token == "all":
            out |= names
        elif token == "mandatory":
            out |= {by_letter[c] for c in MANDATORY}
        else:
            raise ValueError(f"unknown QC check {raw!r}; known: {sorted(names)} or letters a-h")
    return out


# --- shared helpers -----------------------------------------------------------


def _ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def _master_path(proj: Project) -> str | None:
    rel = (proj.get("output") or {}).get("path") or "out/final.mp4"
    path = proj.resolve(rel)
    return path if os.path.isfile(path) and os.path.getsize(path) > 0 else None


def _pcm_master_path(proj: Project) -> str | None:
    """The one-pass pcm_s24le audio render, if the audio lane left it on disk.

    Preferred over the muxed master for check (b): PCM has no priming samples,
    so the comparison can be exact instead of tolerant.
    """
    # out/.work/audio_norm.wav first: that is the file the muxer was actually
    # handed. audio_raw.wav is the pre-loudnorm one-pass render, which still
    # answers the sample-count question if loudnorm was skipped.
    for rel in (
        "out/.work/audio_norm.wav",
        "out/.work/audio_raw.wav",
        "out/audio.wav",
        "out/master.wav",
        "out/audio_master.wav",
        "out/timeline.wav",
    ):
        path = proj.resolve(rel)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def segment_cache_path(proj: Project, seg: dict) -> str | None:
    """Resolve a segment's cached render without knowing which container it used.

    Public because ``ve status`` reports cache warmth from exactly this lookup;
    two definitions of "warm" would drift.
    """
    render = seg.get("render") or {}
    direct = render.get("path")
    if direct:
        path = proj.resolve(direct)
        if os.path.isfile(path):
            return path
    key = render.get("cache_key")
    if not key:
        return None
    try:
        from .cache import Cache
    except ImportError:  # pragma: no cover - cache ships with the package
        return None
    cache = Cache()
    for ext in _SEG_EXT_CANDIDATES:
        try:
            path = cache.path_for("seg", key, ext)
        except VeditError:
            return None
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _overlay_paths(proj: Project) -> list[tuple[dict, str]]:
    out: list[tuple[dict, str]] = []
    for ovl in proj.get("overlays") or []:
        render = ovl.get("render") or {}
        path = render.get("path")
        if path:
            resolved = proj.resolve(path)
            if os.path.isfile(resolved):
                out.append((ovl, resolved))
                continue
        key = render.get("cache_key")
        if not key:
            continue
        try:
            from .cache import Cache

            cache = Cache()
            for ext in (".mov", ".mkv", ".png"):
                candidate = cache.path_for("ovl", key, ext)
                if os.path.isfile(candidate):
                    out.append((ovl, candidate))
                    break
        except (VeditError, ImportError):
            continue
    return out


def _extract_raw(src: str, out_path: str, vf: str, *, pix_fmt: str = "rgba") -> str:
    """Decode exactly one frame of ``src`` through ``vf`` into a raw pixel file.

    Raw rather than PNG because check (d) has to read the alpha byte itself, and
    every PNG decoder in the stdlib would need a third-party dependency the
    pipeline is forbidden from having.
    """
    _ensure_dir(os.path.dirname(out_path))
    run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y",
            "-i", src,
            "-vf", vf,
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", pix_fmt,
            out_path,
        ]
    )
    return out_path


def _filmstrip(src: str, out_path: str, *, tiles: int = 8, width: int = 240) -> str | None:
    """A single PNG contact sheet of ``tiles`` evenly spaced frames.

    This is the artifact that turns "expected 26430 frames, got 26221" into
    something a human can look at.
    """
    try:
        info = probe(src)
    except (VeditError, OSError):
        return None
    duration = float(info.get("duration_s") or 0.0)
    if duration <= 0:
        return None
    step = max(duration / tiles, 1.0 / 30.0)
    _ensure_dir(os.path.dirname(out_path))
    try:
        run(
            [
                FFMPEG, "-hide_banner", "-nostdin", "-y",
                "-i", src,
                "-vf", f"fps=1/{step:.6f},scale={width}:-2,tile={tiles}x1",
                "-frames:v", "1",
                out_path,
            ]
        )
    except (VeditError, OSError):
        return None
    return out_path if os.path.isfile(out_path) else None


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- (a) frame count ----------------------------------------------------------


def _check_frame_count(proj: Project, ev: str) -> list[Finding]:
    """Output frame count == sum of (out_frame - in_frame) over enabled segments.

    The comparison is against ``_computed.total_frames``, which is that sum
    (``speed`` aside -- a retimed segment genuinely occupies a different number
    of timeline frames and pretending otherwise would put every later segment at
    the wrong position). Both numbers are reported so a mismatch caused by a
    speed change is distinguishable from one caused by a dropped frame.
    """
    segs = proj.enabled_segments()
    if not segs:
        return [_f("info", "qc.frame_count.skipped", "render", "no enabled segments; nothing to count")]

    raw_sum = sum(int(s["src_out_frames"]) - int(s["src_in_frames"]) for s in segs)
    expected = proj.total_frames
    tb = proj.timebase()

    master = _master_path(proj)
    if master is None:
        return [
            _f(
                "info",
                "qc.frame_count.no_master",
                "render",
                f"no master render yet at {(proj.get('output') or {}).get('path')!r}; "
                f"timeline expects {expected} frames ({tb.secs(expected)} s) "
                f"over {len(segs)} enabled segments",
            )
        ]

    actual = count_frames(master)
    if actual == expected:
        return [
            _f(
                "info",
                "qc.frame_count.ok",
                "render",
                f"{actual} frames == sum(out-in)={raw_sum} over {len(segs)} enabled segments",
            )
        ]

    delta = actual - expected
    drift_s = abs(delta) * tb.fps_den / tb.fps_num
    strip = _filmstrip(master, os.path.join(ev, "frame_count_master.png"))
    speed_note = "" if raw_sum == expected else f" (raw sum of out-in is {raw_sum}; speed changes account for the difference)"
    return [
        _f(
            "fail",
            "qc.frame_count.mismatch",
            "render",
            f"master has {actual} frames, timeline expects {expected}{speed_note}: "
            f"delta {delta:+d} frames = {drift_s:.3f} s of drift",
            strip,
        )
    ]


# --- (b) audio sample count ---------------------------------------------------


def _check_audio_samples(proj: Project, ev: str) -> list[Finding]:
    """Audio sample count == frames / fps * 48000, computed as exact rationals.

    Checked against the pcm_s24le one-pass render when it survives, because that
    comparison can be exact. Against the muxed master a small head tolerance is
    unavoidable: AAC carries priming samples no muxer strips exactly.
    """
    total_frames = proj.total_frames
    if total_frames <= 0:
        return [_f("info", "qc.audio_samples.skipped", "audio", "empty timeline; nothing to count")]

    tb = proj.timebase()
    expected = tb.frames_to_samples(total_frames)

    pcm = _pcm_master_path(proj)
    target = pcm or _master_path(proj)
    if target is None:
        return [
            _f(
                "info",
                "qc.audio_samples.no_master",
                "audio",
                f"no rendered audio yet; timeline expects {expected} samples "
                f"@ {tb.sample_rate} Hz for {total_frames} frames",
            )
        ]

    actual = count_audio_samples(target)
    if actual == 0:
        return [_f("fail", "qc.audio_samples.silent", "audio", f"{target} has no decodable audio stream")]

    delta = actual - expected
    name = os.path.basename(target)
    if delta == 0:
        return [_f("info", "qc.audio_samples.ok", "audio", f"{name}: {actual} samples == frames/fps*{tb.sample_rate}")]

    tolerance = 0 if pcm else _LOSSY_AUDIO_TOLERANCE_SAMPLES
    one_frame = tb.frames_to_samples(1)
    drift_ms = abs(delta) * 1000.0 / tb.sample_rate

    if abs(delta) <= tolerance:
        return [
            _f(
                "info",
                "qc.audio_samples.ok",
                "audio",
                f"{name}: {actual} samples, {delta:+d} vs expected {expected} "
                f"({drift_ms:.2f} ms) -- within codec priming tolerance",
            )
        ]
    severity = "warn" if abs(delta) <= one_frame else "fail"
    return [
        _f(
            severity,
            "qc.audio_samples.mismatch",
            "audio",
            f"{name}: {actual} samples, expected {expected} for {total_frames} frames "
            f"({delta:+d} = {drift_ms:.2f} ms). "
            + ("Under one frame, but a real edit accumulates this." if severity == "warn"
               else "Audio and video will end visibly out of sync."),
        )
    ]


# --- (c) concat parameter match ----------------------------------------------


def _check_concat_params(proj: Project, ev: str) -> list[Finding]:
    """ffprobe parameter match across all segments BEFORE any -c copy concat.

    The concat demuxer does not validate its inputs: on a codec, geometry,
    pix_fmt or audio-layout mismatch it writes a corrupt file and exits 0. There
    is no later stage at which this becomes visible as an error.
    """
    segs = proj.enabled_segments()
    if not segs:
        return [_f("info", "qc.concat_params.skipped", "render", "no enabled segments")]

    paths: list[str] = []
    cold: list[str] = []
    for seg in segs:
        path = segment_cache_path(proj, seg)
        if path:
            paths.append(path)
        else:
            cold.append(str(seg.get("id")))

    findings: list[Finding] = []
    if cold:
        shown = ", ".join(cold[:6]) + (f", +{len(cold) - 6} more" if len(cold) > 6 else "")
        findings.append(
            _f(
                "info",
                "qc.concat_params.cache_cold",
                "render",
                f"{len(cold)}/{len(segs)} enabled segments have no cached render yet ({shown})",
            )
        )
    if len(paths) < 2:
        findings.append(
            _f("info", "qc.concat_params.skipped", "render", f"only {len(paths)} cached segment(s); nothing to compare")
        )
        return findings

    ok, reason = concat_compatible(paths)
    if ok:
        findings.append(_f("info", "qc.concat_params.ok", "render", f"{reason}; safe for -f concat -c copy"))
    else:
        report = os.path.join(_ensure_dir(ev), "concat_params.txt")
        with open(report, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(reason + "\n\n")
            for path in paths:
                try:
                    info = probe(path)
                except (VeditError, OSError) as exc:
                    fh.write(f"{path}\tUNPROBEABLE {exc}\n")
                    continue
                fh.write(
                    f"{path}\t{info.get('v_codec')}\t{info.get('width')}x{info.get('height')}\t"
                    f"{info.get('pix_fmt')}\t{info.get('fps_num')}/{info.get('fps_den')}\t"
                    f"{info.get('a_codec')}\t{info.get('sample_rate')}\t{info.get('channels')}\n"
                )
        findings.append(
            _f(
                "fail",
                "qc.concat_params.mismatch",
                "render",
                f"segment renders differ -- -c copy would emit a corrupt file with exit 0: {reason}",
                report,
            )
        )
    return findings


# --- (d) alpha probe ----------------------------------------------------------


def _check_alpha(proj: Project, ev: str) -> list[Finding]:
    """One raw rgba pixel of a graphic must read a=0.

    Top-left corner of frame 0. A lower third that fills its own background
    reads a=255 there and will paint a black box over the footage; a player that
    composites over its own checkerboard makes that look correct in preview.
    """
    overlays = _overlay_paths(proj)
    if not overlays:
        return [_f("info", "qc.alpha.skipped", "graphics", "no rendered overlays to probe")]

    findings: list[Finding] = []
    for ovl, path in overlays:
        oid = str(ovl.get("id"))
        try:
            info = probe(path)
        except (VeditError, OSError) as exc:
            findings.append(_f("fail", "qc.alpha.unprobeable", "graphics", f"{oid}: cannot probe {path}: {exc}"))
            continue
        pix_fmt = str(info.get("pix_fmt") or "")
        if pix_fmt not in _ALPHA_PIX_FMTS:
            findings.append(
                _f(
                    "fail",
                    "qc.alpha.opaque_pix_fmt",
                    "graphics",
                    f"{oid}: {os.path.basename(path)} is {pix_fmt or 'unknown'} which carries no alpha "
                    f"(use qtrle/argb or ProRes4444; VP9-in-WebM does not survive this pipeline)",
                    path,
                )
            )
            continue
        raw = os.path.join(_ensure_dir(ev), f"alpha_{oid}.rgba")
        _extract_raw(path, raw, "format=rgba,crop=1:1:0:0")
        with open(raw, "rb") as fh:
            pixel = fh.read(4)
        if len(pixel) < 4:
            findings.append(_f("fail", "qc.alpha.no_pixel", "graphics", f"{oid}: could not read a pixel from {path}", raw))
            continue
        r, g, b, a = pixel
        if a == 0:
            findings.append(
                _f("info", "qc.alpha.ok", "graphics", f"{oid}: top-left rgba=({r},{g},{b},{a}) -- transparent")
            )
        else:
            findings.append(
                _f(
                    "fail",
                    "qc.alpha.opaque",
                    "graphics",
                    f"{oid}: top-left rgba=({r},{g},{b},{a}); alpha must be 0. "
                    f"The overlay will composite as a solid box.",
                    raw,
                )
            )
    return findings


# --- (e) font fallback --------------------------------------------------------

# libass logs "Loading font file ..." and then silently falls back to
# DirectWrite/Arial when the family name does not match, exiting 0. The rendered
# frame is then BYTE-IDENTICAL to an Arial control -- measured, both md5
# 59e7be04. Hash equality is the only signal that catches it.

_FONT_PROBE_TEXT = r"Größe Straße Übermäßig 123"

_ASS_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 200
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: P,{fontname},48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,3,0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,P,,0,0,0,,{text}
"""


def _caption_fontname(proj: Project) -> tuple[str, str, str]:
    """(fontname, bold, where it came from).

    The sidecar ASS is the truth when it exists -- it is literally what libass
    will be handed. A preset JSON is second best, and the contract's default
    caption face is last.
    """
    caps = proj.get("captions") or {}
    sidecar = caps.get("sidecar_path")
    if sidecar:
        path = proj.resolve(sidecar)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("Style:"):
                            fields = [x.strip() for x in line[len("Style:"):].split(",")]
                            if len(fields) >= 8:
                                return fields[1], fields[7], os.path.basename(path)
            except OSError:
                pass
    preset = caps.get("preset")
    if preset:
        from .keys import repo_root

        preset_path = os.path.join(repo_root(), "presets", "captions", f"{preset}.json")
        if os.path.isfile(preset_path):
            try:
                import json

                with open(preset_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                style = data.get("style") if isinstance(data.get("style"), dict) else data
                name = style.get("Fontname") or style.get("fontname") or style.get("font")
                if name:
                    bold = str(style.get("Bold", style.get("bold", 0)))
                    return str(name), bold, f"presets/captions/{preset}.json"
            except (OSError, ValueError):
                pass
    # Contract section 7: a static single-weight face, selected by its exact
    # nameID 1 string with Bold: 0. Never a variable font.
    return "Montserrat ExtraBold", "0", "contract default"


def _render_caption_probe(fontname: str, bold: str, fonts_dir: str, out_png: str, ass_path: str) -> None:
    ass = _ASS_TEMPLATE.format(fontname=fontname, bold=bold, text=_FONT_PROBE_TEXT)
    with open(ass_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ass)
    vf = f"subtitles={escape_filter_path(ass_path)}:fontsdir={escape_filter_path(fonts_dir)}"
    run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=640x200:r=30:d=0.1",
            "-vf", vf,
            "-frames:v", "1",
            out_png,
        ]
    )


def _check_font_fallback(proj: Project, ev: str) -> list[Finding]:
    caps = proj.get("captions") or {}
    if not caps.get("enabled"):
        return [_f("info", "qc.font_fallback.skipped", "captions", "captions are disabled for this project")]

    from .keys import repo_root

    fonts_dir = os.path.join(repo_root(), "assets", "fonts")
    if not os.path.isdir(fonts_dir):
        return [_f("fail", "qc.font_fallback.no_fontsdir", "captions", f"no font directory at {fonts_dir}")]

    # libass opens EVERY file in fontsdir and errors on non-fonts, so a stray
    # README in there is a real defect, not tidiness.
    strays = [
        n for n in os.listdir(fonts_dir)
        if os.path.isfile(os.path.join(fonts_dir, n)) and os.path.splitext(n)[1].lower() not in (".ttf", ".otf", ".ttc")
    ]

    fontname, bold, origin = _caption_fontname(proj)
    ev = _ensure_dir(ev)
    probe_png = os.path.join(ev, "font_probe.png")
    control_png = os.path.join(ev, "font_control_arial.png")
    # The control MUST carry the same Bold value. Comparing a Bold=-1 probe
    # against a Bold=0 Arial control makes every render differ and the check
    # passes on a face that fell back -- a false green, which is worse than no
    # check at all.
    _render_caption_probe(fontname, bold, fonts_dir, probe_png, os.path.join(ev, "font_probe.ass"))
    _render_caption_probe("Arial", bold, fonts_dir, control_png, os.path.join(ev, "font_control.ass"))

    findings: list[Finding] = []
    if strays:
        findings.append(
            _f(
                "warn",
                "qc.font_fallback.stray_files",
                "captions",
                f"assets/fonts contains non-font files libass will try to open: {', '.join(sorted(strays)[:8])}",
                fonts_dir,
            )
        )

    probe_hash = _sha256_file(probe_png)
    control_hash = _sha256_file(control_png)
    if probe_hash == control_hash:
        findings.append(
            _f(
                "fail",
                "qc.font_fallback.silent_fallback",
                "captions",
                f"caption face {fontname!r} (Bold={bold}, from {origin}) rendered byte-identical to an Arial "
                f"control ({probe_hash[:12]}): libass fell back silently. Use the exact nameID 1 string of a "
                f"static font file in assets/fonts.",
                probe_png,
            )
        )
    else:
        findings.append(
            _f(
                "info",
                "qc.font_fallback.ok",
                "captions",
                f"caption face {fontname!r} (from {origin}) differs from the Arial control "
                f"({probe_hash[:12]} vs {control_hash[:12]})",
                probe_png,
            )
        )
    return findings


# --- (f) loudness -------------------------------------------------------------

_EBUR_I = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)
_EBUR_LRA = re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", re.M)
_EBUR_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.M)


def _check_loudness(proj: Project, ev: str) -> list[Finding]:
    """ebur128 on the master against the platform target stored in project.json.

    The target lives in ``output.loudnorm`` and is derived from
    ``output.platform`` at project creation, so this check never hardcodes
    YouTube's -14 LUFS the way every earlier draft of this pipeline did.
    """
    master = _master_path(proj)
    if master is None:
        return [_f("info", "qc.loudness.no_master", "master", "no master render yet")]

    out = proj.get("output") or {}
    target = out.get("loudnorm") or {}
    want_i = float(target.get("I", -14.0))
    want_tp = float(target.get("TP", -1.5))

    proc = run(
        [FFMPEG, "-hide_banner", "-nostats", "-nostdin", "-i", master, "-af", "ebur128=peak=true", "-f", "null", "-"],
        check=False,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    report = os.path.join(_ensure_dir(ev), "loudness.txt")
    with open(report, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text[-8000:])

    i_vals = _EBUR_I.findall(text)
    peak_vals = _EBUR_PEAK.findall(text)
    lra_vals = _EBUR_LRA.findall(text)
    if not i_vals:
        return [_f("fail", "qc.loudness.unmeasurable", "master", f"ebur128 produced no summary for {master}", report)]

    got_i = float(i_vals[-1])
    got_tp = float(peak_vals[-1]) if peak_vals else float("nan")
    got_lra = float(lra_vals[-1]) if lra_vals else float("nan")
    platform = out.get("platform", "youtube")

    findings: list[Finding] = []
    delta_i = got_i - want_i
    if abs(delta_i) <= 1.0:
        findings.append(
            _f(
                "info",
                "qc.loudness.ok",
                "master",
                f"{platform}: I={got_i:.1f} LUFS (target {want_i:.1f}, {delta_i:+.1f} LU), "
                f"TP={got_tp:.1f} dBFS, LRA={got_lra:.1f} LU",
                report,
            )
        )
    else:
        severity = "warn" if abs(delta_i) <= 2.0 else "fail"
        findings.append(
            _f(
                severity,
                "qc.loudness.off_target",
                "master",
                f"{platform}: I={got_i:.1f} LUFS, target {want_i:.1f} ({delta_i:+.1f} LU). "
                f"{platform} will normalize this and the mix will not sound as delivered.",
                report,
            )
        )
    if peak_vals and got_tp > want_tp + 0.3:
        findings.append(
            _f(
                "fail",
                "qc.loudness.true_peak",
                "master",
                f"true peak {got_tp:.1f} dBFS exceeds the {want_tp:.1f} dBFS ceiling; "
                f"lossy transcode on the platform will clip",
                report,
            )
        )
    return findings


# --- (g) chrome blank page ----------------------------------------------------


def _check_chrome_blank(proj: Project, ev: str) -> list[Finding]:
    """A rendered graphic frame must not be a uniform page.

    ``chrome --headless --screenshot`` fails SILENTLY on a path it cannot open:
    exit 0, plausible file size, and three different HTML files producing one
    identical hash of a blank dark-mode error page. Uniformity is the detectable
    signature, and it needs no known-good control hash to work.

    A control hash is still honoured when the graphics lane leaves one at
    ``.cache/html/blank_page.sha256`` -- that catches a blank page that happens
    to carry a gradient.

    SEVERAL frames are sampled, not just frame 0. Every graphic is a pure
    function of ?t and the templates in presets/graphics/ all open at opacity 0,
    so frame 0 of a working lower third IS uniform and transparent. Judging on
    frame 0 alone failed a correct render -- measured on the integration
    fixture. The overlay is blank only if EVERY sampled frame is uniform.
    """
    overlays = _overlay_paths(proj)
    if not overlays:
        return [_f("info", "qc.chrome_blank.skipped", "graphics", "no rendered overlays to inspect")]

    from .keys import default_cache_root

    control_file = os.path.join(default_cache_root(), "html", "blank_page.sha256")
    control_hash = None
    if os.path.isfile(control_file):
        try:
            with open(control_file, "r", encoding="utf-8") as fh:
                control_hash = fh.read().strip().split()[0]
        except (OSError, IndexError):
            control_hash = None

    findings: list[Finding] = []
    for ovl, path in overlays:
        oid = str(ovl.get("id"))
        frames = int((ovl.get("render") or {}).get("frames") or ovl.get("duration_frames") or 1)
        # First, middle and three-quarter frames: an entrance that starts blank
        # and an exit that ends blank both have a lit frame in between.
        probes = sorted({0, max(0, frames // 2), max(0, (frames * 3) // 4)})
        png = os.path.join(_ensure_dir(ev), f"blank_{oid}.png")
        best = -1
        best_png_written = False
        undecodable: str | None = None
        for n in probes:
            try:
                raw = os.path.join(ev, f"blank_{oid}_{n}.rgba")
                _extract_raw(path, raw, f"select=eq(n\\,{n}),scale=160:-2,format=rgba")
                with open(raw, "rb") as fh:
                    data = fh.read()
            except (VeditError, OSError) as exc:
                undecodable = str(exc)
                continue
            distinct = len({data[i : i + 4] for i in range(0, len(data) - 3, 4)})
            if distinct > best:
                best = distinct
                try:
                    run([FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", path,
                         "-vf", f"select=eq(n\\,{n})", "-vsync", "0", "-frames:v", "1", png])
                    best_png_written = True
                except (VeditError, OSError):
                    best_png_written = best_png_written  # evidence is optional
        if best < 0:
            findings.append(
                _f("fail", "qc.chrome_blank.undecodable", "graphics", f"{oid}: {undecodable}")
            )
            continue
        probe_list = ",".join(str(n) for n in probes)
        if best <= 1:
            findings.append(
                _f(
                    "fail",
                    "qc.chrome_blank.uniform",
                    "graphics",
                    f"{oid}: frames {probe_list} are each a single flat colour -- the screenshot is a "
                    f"blank page (chrome exits 0 on a file:// URL it cannot open)",
                    png if best_png_written else None,
                )
            )
            continue
        if control_hash and best_png_written and _sha256_file(png) == control_hash:
            findings.append(
                _f(
                    "fail",
                    "qc.chrome_blank.matches_control",
                    "graphics",
                    f"{oid}: hashes identical to the known blank-page control",
                    png,
                )
            )
            continue
        findings.append(
            _f("info", "qc.chrome_blank.ok", "graphics",
               f"{oid}: {best} distinct pixels at the busiest of frames {probe_list}",
               png if best_png_written else None)
        )
    return findings


# --- (h) calibration ----------------------------------------------------------


def _check_calibration(proj: Project, ev: str) -> list[Finding]:
    """Warn while thresholds are research-derived rather than measured.

    Not one of the mandatory seven, and never a failure. It exists so a green QC
    run cannot be read as "these cut thresholds were validated" when no fixture
    has ever been run through them.
    """
    if proj.get("calibrated") is True:
        return [_f("info", "qc.calibration.ok", "project", "thresholds marked calibrated against a real fixture")]
    return [
        _f(
            "warn",
            "qc.calibration.uncalibrated",
            "project",
            "calibrated=false: every cut threshold is research-derived and has not been checked against real "
            "German talking-head footage. Cuts may be perceptually wrong even when every frame count matches.",
        )
    ]


# --- rendering findings for humans -------------------------------------------


def format_findings(result: dict[str, Any], *, width: int | None = None) -> str:
    """A readable table. The CLI prints this; a hook can capture it verbatim."""
    findings = result.get("findings") or []
    if width is None:
        width = max(shutil.get_terminal_size((120, 25)).columns, 80)

    header = ("SEV", "CODE", "STAGE", "MESSAGE")
    rows = [
        (
            f["severity"].upper(),
            f["code"],
            f["stage"],
            f["message"],
            f.get("evidence_path") or "",
        )
        for f in findings
    ]

    sev_w = max([len(header[0])] + [len(r[0]) for r in rows]) if rows else len(header[0])
    code_w = max([len(header[1])] + [len(r[1]) for r in rows]) if rows else len(header[1])
    stage_w = max([len(header[2])] + [len(r[2]) for r in rows]) if rows else len(header[2])
    msg_w = max(30, width - (sev_w + code_w + stage_w + 6))

    lines: list[str] = []
    lines.append(f"{header[0]:<{sev_w}}  {header[1]:<{code_w}}  {header[2]:<{stage_w}}  {header[3]}")
    lines.append("-" * min(width, sev_w + code_w + stage_w + 6 + msg_w))
    for sev, code, stage, message, evidence in rows:
        chunks = _wrap(message, msg_w)
        lines.append(f"{sev:<{sev_w}}  {code:<{code_w}}  {stage:<{stage_w}}  {chunks[0]}")
        pad = " " * (sev_w + code_w + stage_w + 6)
        for chunk in chunks[1:]:
            lines.append(pad + chunk)
        if evidence:
            lines.append(pad + "look: " + evidence)

    counts = result.get("counts") or {}
    lines.append("")
    lines.append(
        f"{counts.get('fail', 0)} failure(s), {counts.get('warn', 0)} warning(s), "
        f"{counts.get('info', 0)} note(s) -- exit {result.get('exit_code', 0)}"
    )
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    out: list[str] = []
    line = words[0]
    for word in words[1:]:
        if len(line) + 1 + len(word) <= width:
            line += " " + word
        else:
            out.append(line)
            line = word
    out.append(line)
    return out
