"""The dialogue bed, the music bed, and loudness -- rendered in ONE pass.

Why one pass (CONTRACT section 6): every per-segment audio cache measured in the
research drifts at the junctions. Three 1.000000 s AAC segments concat-copied
came out at 3.021333 s (+10.67 ms per join); a 4-junction rough cut over
per-segment AAC gained 5760 samples (+120 ms). There is no drift here because
there are no joins -- ``adelay`` places every segment at an absolute sample
index inside one continuous graph.

The graph shape is fixed::

    [n:a] aformat -> atrim -> asetpts -> afade(in) -> afade(out) -> volume
                  -> adelay=<n>S -> amix=normalize=0 -> apad/atrim -> pcm_s24le

Four traps encoded here, each measured, not assumed:

- ``adelay`` without a suffix takes integer MILLISECONDS. Timeline positions at
  30000/1001 are not integer ms, so every segment would round-drift. The ``S``
  suffix takes samples and is the only spelling used in this module.
- ``amix`` defaults to ``normalize=1`` and divides by the input count: adding a
  quiet music bed silently costs the voice ~6 dB (-14.3 -> -20.3 measured).
- ``loudnorm`` silently resamples its output to 192000 Hz unless pass 2 carries
  an explicit ``-ar``.
- ``acrossfade`` defaults to ``o=1`` and SHORTENS the join by exactly ``d``.
  This module never uses it; splice hygiene is explicit ``afade`` + ``adelay``
  arithmetic that we own.

Audio timing is expressed in SAMPLES everywhere (``atrim=start_sample=``,
``afade=ss=/ns=``), never seconds. Frame boundaries at 30000/1001 do not land on
sample boundaries, and a float second re-parsed by ffmpeg is a rounding event.
"""

from __future__ import annotations

import array
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .ffmpeg import FFMPEG, VeditError, filter_complex_args, run, write_filtergraph
from .keys import default_cache_root
from .probe import probe
from .project import Project
from .timebase import Timebase

# CONTRACT section 8: research-derived, NOT perceptually calibrated.
DEFAULT_FADE_MS = 20.0
ZERO_CROSS_SEARCH_MS = 5.0

# Everything is forced onto one interchange format before it is touched. Mixing
# a mono lavalier against a stereo music bed otherwise makes amix guess, and
# sidechaincompress refuses mismatched layouts outright.
_SAMPLE_FMT = "fltp"


class AudioError(VeditError):
    """The audio graph could not be built or rendered."""


# --- small helpers ------------------------------------------------------------


def _num(value: float) -> str:
    """A float for a filter argument: no exponent, no trailing noise, no comma.

    ``repr(1e-05)`` is ``'1e-05'`` and ffmpeg's option parser reads the ``e`` as
    the start of a unit suffix. Fixed notation avoids the whole question.
    """
    return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"


def _db(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


def _seg_audio(seg: dict) -> dict:
    return seg.get("audio") or {}


def _audio_source_range(seg: dict) -> tuple[int, int, int, int]:
    """Audio in/out frames for one segment, plus its J/L lead and tail.

    ``audio.src_in_frames = null`` means "mirror the video" (schema v1.1).
    ``lead_frames`` (J-cut) pulls the audio in-point earlier on BOTH the source
    and the timeline; ``tail_frames`` (L-cut) extends the out-point later. The
    video cut never moves -- that is the entire point of keeping these here
    instead of in ``src_in_frames``.
    """
    a = _seg_audio(seg)
    src_in = a.get("src_in_frames")
    src_out = a.get("src_out_frames")
    a_in = int(seg["src_in_frames"] if src_in is None else src_in)
    a_out = int(seg["src_out_frames"] if src_out is None else src_out)
    lead = int(a.get("lead_frames", 0) or 0)
    tail = int(a.get("tail_frames", 0) or 0)
    return a_in - lead, a_out + tail, lead, tail


@dataclass
class AudioPlan:
    """What the graph is going to do, in samples. Inspectable before rendering.

    Kept as a separate object because every bug in this lane is an arithmetic
    bug, and an arithmetic bug you can print is an arithmetic bug you can fix.
    """

    total_samples: int
    branches: list[dict[str, Any]] = field(default_factory=list)
    music: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    exact_length: bool = True


# --- the graph ----------------------------------------------------------------


def build_audio_plan(project: Project, *, exact_length: bool = True) -> AudioPlan:
    """Resolve every segment and music track to absolute sample indices."""
    tb = project.timebase()
    total_frames = project.total_frames
    total_samples = tb.frames_to_samples(total_frames)

    plan = AudioPlan(total_samples=total_samples, exact_length=exact_length)

    # One -i per distinct FILE, not per segment. ffmpeg auto-inserts a split
    # when an input pad is referenced more than once, so a 180-segment timeline
    # off one camera decodes the source exactly once.
    index_of: dict[str, int] = {}

    def input_index(path: str) -> int:
        key = os.path.normcase(os.path.abspath(path))
        if key not in index_of:
            index_of[key] = len(plan.inputs)
            plan.inputs.append(path)
        return index_of[key]

    vo_gain = 0.0
    for track in project.data.get("audio_tracks") or []:
        if track.get("source") == "segments" or track.get("kind") == "dialogue":
            vo_gain = float(track.get("gain_db", 0.0) or 0.0)
            break

    for seg in project.enabled_segments():
        seg_id = str(seg.get("id"))
        pos = project.position(seg_id)
        src = project.source(str(seg.get("source_id")))
        path = project.resolve(str(src["path"]))
        a = _seg_audio(seg)

        a_in_f, a_out_f, lead, _tail = _audio_source_range(seg)
        if a_in_f < 0:
            # A J-cut at the head of a take has nothing to lead into.
            a_in_f = 0

        start_sample = tb.frames_to_samples(a_in_f)
        end_sample = tb.frames_to_samples(a_out_f)

        # Sub-frame, audio only, never moves the video (CONTRACT section 8).
        nudge = int(a.get("zero_cross_offset_samples", 0) or 0)
        start_sample += nudge
        end_sample += nudge
        if start_sample < 0:
            end_sample -= start_sample
            start_sample = 0
        if end_sample <= start_sample:
            raise AudioError(f"segment {seg_id!r} has an empty audio range after nudging")

        delay = tb.frames_to_samples(pos["tl_in"] - lead)
        if delay < 0:
            delay = 0

        length = end_sample - start_sample
        fade_in = _fade_samples(a.get("fade_in_ms", DEFAULT_FADE_MS), tb, length)
        fade_out = _fade_samples(a.get("fade_out_ms", DEFAULT_FADE_MS), tb, length)

        plan.branches.append(
            {
                "id": seg_id,
                "input": input_index(path),
                "start_sample": start_sample,
                "end_sample": end_sample,
                "length": length,
                "delay": delay,
                "fade_in": fade_in,
                "fade_out": fade_out,
                # tri = linear/constant-amplitude, correct for correlated room
                # tone from the same take; qsin = equal-power, for a mic change.
                "curve": str(a.get("fade_curve", "tri")),
                "gain_db": float(a.get("gain_db", 0.0) or 0.0) + vo_gain,
            }
        )

    for track in project.data.get("audio_tracks") or []:
        if track.get("kind") != "music" or not track.get("path"):
            continue
        path = project.resolve(str(track["path"]))
        if not os.path.isfile(path):
            raise AudioError(f"music track {track.get('id')!r} points at a missing file: {path}")
        info = probe(path)
        if not info["has_audio"]:
            raise AudioError(f"music track {track.get('id')!r} has no audio stream: {path}")
        src_rate = int(info["sample_rate"] or tb.sample_rate)
        available = int(info["duration_s"] * tb.sample_rate)

        delay = tb.frames_to_samples(int(track.get("timeline_in_frames", 0) or 0))
        needed = max(0, total_samples - delay)
        length = min(available, needed) if needed else 0
        if length <= 0:
            continue

        fade_in = min(int(float(track.get("fade_in_s", 0.0) or 0.0) * tb.sample_rate), length // 2)
        fade_out = min(int(float(track.get("fade_out_s", 0.0) or 0.0) * tb.sample_rate), length // 2)

        plan.music.append(
            {
                "id": str(track.get("id", "music")),
                "input": input_index(path),
                "src_rate": src_rate,
                "length": length,
                "delay": delay,
                "fade_in": max(0, fade_in),
                "fade_out": max(0, fade_out),
                "gain_db": float(track.get("gain_db", 0.0) or 0.0),
                "ducking": dict(track.get("ducking") or {}),
            }
        )

    return plan


def _fade_samples(ms: Any, tb: Timebase, length: int) -> int:
    """Fade length in samples, clamped so in+out cannot exceed the segment.

    A 20 ms fade on a 15 ms filler removal would otherwise make afade's out
    window start before its in window ends and produce a notch.
    """
    try:
        value = float(ms)
    except (TypeError, ValueError):
        value = DEFAULT_FADE_MS
    samples = int(value / 1000.0 * tb.sample_rate + 0.5)
    return max(0, min(samples, length // 2))


def build_audio_graph(project: Project) -> tuple[str, list[str]]:
    """Return ``(filtergraph, input_args)`` for the whole timeline in one pass.

    ``input_args`` is a ready-to-splice argv fragment (``["-i", p1, "-i", p2]``)
    whose order defines the ``[n:a]`` indices the graph refers to.
    """
    plan = build_audio_plan(project)
    graph = render_plan_graph(plan, project.timebase())
    args: list[str] = []
    for path in plan.inputs:
        args += ["-i", path]
    return graph, args


def render_plan_graph(plan: AudioPlan, tb: Timebase) -> str:
    """Serialize an AudioPlan into the filtergraph text."""
    if not plan.branches and not plan.music:
        raise AudioError("nothing to render: no enabled segments and no music track")

    lines: list[str] = []
    labels: list[str] = []
    fmt = f"aformat=sample_fmts={_SAMPLE_FMT}:sample_rates={tb.sample_rate}:channel_layouts=stereo"

    for i, b in enumerate(plan.branches):
        label = f"s{i}"
        chain = [
            fmt,
            f"atrim=start_sample={b['start_sample']}:end_sample={b['end_sample']}",
            "asetpts=PTS-STARTPTS",
        ]
        if b["fade_in"] > 0:
            chain.append(f"afade=t=in:ss=0:ns={b['fade_in']}:curve={b['curve']}")
        if b["fade_out"] > 0:
            start = b["length"] - b["fade_out"]
            chain.append(f"afade=t=out:ss={start}:ns={b['fade_out']}:curve={b['curve']}")
        if abs(b["gain_db"]) > 1e-9:
            chain.append(f"volume={_db(b['gain_db'])}dB")
        # ``all=1`` or only channel 0 is delayed and the bed goes out of phase.
        chain.append(f"adelay={b['delay']}S:all=1")
        lines.append(f"[{b['input']}:a]" + ",".join(chain) + f"[{label}]")
        labels.append(label)

    if len(labels) > 1:
        lines.append(
            "".join(f"[{x}]" for x in labels)
            + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[vo]"
        )
        vo = "vo"
    elif labels:
        # amix=inputs=1 is legal but pointless; anull keeps the label uniform.
        lines.append(f"[{labels[0]}]anull[vo]")
        vo = "vo"
    else:
        vo = ""

    music_labels: list[str] = []
    duck: dict[str, Any] | None = None
    for i, m in enumerate(plan.music):
        label = f"m{i}"
        chain = [
            fmt,
            f"atrim=start_sample=0:end_sample={m['length']}",
            "asetpts=PTS-STARTPTS",
        ]
        if abs(m["gain_db"]) > 1e-9:
            chain.append(f"volume={_db(m['gain_db'])}dB")
        if m["fade_in"] > 0:
            chain.append(f"afade=t=in:ss=0:ns={m['fade_in']}:curve=tri")
        if m["fade_out"] > 0:
            chain.append(f"afade=t=out:ss={m['length'] - m['fade_out']}:ns={m['fade_out']}:curve=tri")
        chain.append(f"adelay={m['delay']}S:all=1")
        lines.append(f"[{m['input']}:a]" + ",".join(chain) + f"[{label}]")
        cfg = m.get("ducking") or {}
        if cfg.get("enabled") and vo and duck is None:
            duck = {"label": label, "cfg": cfg}
        else:
            music_labels.append(label)

    if duck is not None and vo:
        # sidechaincompress: FIRST input is the signal being compressed (music),
        # SECOND is the key (voice). Reversed, you duck the voice under the bed.
        cfg = duck["cfg"]
        lines.append(f"[{vo}]asplit=2[vo_main][vo_key]")
        lines.append(
            f"[{duck['label']}][vo_key]sidechaincompress="
            f"threshold={_num(cfg.get('threshold', 0.03))}:"
            f"ratio={_num(cfg.get('ratio', 8))}:"
            f"attack={_num(cfg.get('attack_ms', 20))}:"
            f"release={_num(cfg.get('release_ms', 400))}:"
            f"makeup={_num(cfg.get('makeup', 1))}[mduck]"
        )
        stems = ["vo_main", "mduck"] + music_labels
    elif vo:
        stems = [vo] + music_labels
    else:
        stems = music_labels

    if len(stems) > 1:
        lines.append(
            "".join(f"[{s}]" for s in stems)
            + f"amix=inputs={len(stems)}:normalize=0:dropout_transition=0[mix]"
        )
        mix = "mix"
    else:
        mix = stems[0]

    if plan.exact_length:
        # The timeline length is defined by the frame math, not by whatever the
        # last branch happens to decode to. apad guarantees we are never short,
        # atrim=end_sample guarantees we are never long, and both are exact
        # integers -- so QC check (b) compares two numbers derived the same way.
        lines.append(
            f"[{mix}]apad=whole_len={plan.total_samples},"
            f"atrim=end_sample={plan.total_samples},asetpts=PTS-STARTPTS[out]"
        )
    else:
        lines.append(f"[{mix}]anull[out]")

    return ";\n".join(lines) + "\n"


# --- rendering ----------------------------------------------------------------


def render_audio(project: Project, out_path: str) -> str:
    """Render the whole timeline to one ``pcm_s24le`` WAV. Returns ``out_path``.

    Lossless on purpose: this file is an intermediate that gets muxed, and AAC
    is encoded exactly once, at the mux (CONTRACT section 6).
    """
    tb = project.timebase()
    graph, inputs = build_audio_graph(project)
    work = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".", ".work")
    fc = write_filtergraph(graph, work)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    channels = int((project.data.get("output") or {}).get("a_channels", 2) or 2)
    argv = [FFMPEG, "-y", "-hide_banner", "-nostats"]
    argv += inputs
    argv += filter_complex_args(fc)
    argv += [
        "-map",
        "[out]",
        "-c:a",
        "pcm_s24le",
        "-ar",
        str(tb.sample_rate),
        "-ac",
        str(channels),
        out_path,
    ]
    run(argv)
    return out_path


_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def loudnorm_two_pass(in_path: str, out_path: str, target: dict) -> dict:
    """Measure, then apply. Returns the measured values and what was written.

    ``linear=true`` is what makes two passes worth doing: one static gain
    instead of the dynamic squashing the single-pass filter applies. ffmpeg
    falls back to dynamic on its own if the required offset is out of range and
    says so on stderr, which is captured here.

    The explicit ``-ar`` is mandatory: without it loudnorm writes 192000 Hz
    (measured), and the mux then silently resamples the master.
    """
    i = float(target.get("I", -14))
    tp = float(target.get("TP", -1.5))
    lra = float(target.get("LRA", 11))
    rate = int(target.get("sample_rate", 48000))

    work = os.path.join(os.path.dirname(os.path.abspath(out_path)) or ".", ".work")

    measure = f"[0:a]loudnorm=I={_num(i)}:TP={_num(tp)}:LRA={_num(lra)}:print_format=json[out]\n"
    fc1 = write_filtergraph(measure, work)
    proc = run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", in_path]
        + filter_complex_args(fc1)
        + ["-map", "[out]", "-f", "null", "-"]
    )
    stats = _parse_loudnorm_json(proc.stderr or "")
    if stats is None:
        raise AudioError(f"loudnorm pass 1 printed no JSON for {in_path}")

    measured_i = _as_float(stats.get("input_i"))
    if measured_i is None or measured_i <= -70.0:
        # Digital silence. loudnorm would apply +infinite gain and produce noise.
        raise AudioError(
            f"refusing to normalize {in_path}: measured integrated loudness is "
            f"{stats.get('input_i')!r} (effectively silent)"
        )

    apply_chain = (
        f"[0:a]loudnorm=I={_num(i)}:TP={_num(tp)}:LRA={_num(lra)}"
        f":measured_I={stats['input_i']}"
        f":measured_TP={stats['input_tp']}"
        f":measured_LRA={stats['input_lra']}"
        f":measured_thresh={stats['input_thresh']}"
        f":offset={stats['target_offset']}"
        ":linear=true[out]\n"
    )
    fc2 = write_filtergraph(apply_chain, work)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    proc2 = run(
        [FFMPEG, "-y", "-hide_banner", "-nostats", "-i", in_path]
        + filter_complex_args(fc2)
        + [
            "-map",
            "[out]",
            "-ar",  # MANDATORY: see docstring.
            str(rate),
            "-c:a",
            "pcm_s24le",
            out_path,
        ]
    )

    return {
        "in_path": in_path,
        "out_path": out_path,
        "target": {"I": i, "TP": tp, "LRA": lra},
        "measured": stats,
        "sample_rate": rate,
        "linear": "dynamic mode" not in (proc2.stderr or "").lower(),
    }


def _parse_loudnorm_json(stderr: str) -> dict[str, Any] | None:
    """loudnorm prints its JSON to stderr among the banner and progress lines."""
    matches = _JSON_BLOCK.findall(stderr)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) or math.isinf(result) else result


# --- zero-crossing snap -------------------------------------------------------


@dataclass(frozen=True)
class ZeroCross:
    """Result of a zero-crossing search. ``delta_samples`` is audio-only."""

    delta_samples: int
    slope: int  # +1 rising through zero, -1 falling, 0 = nothing found
    found: bool
    searched_samples: int


def _decode_s16(argv: list[str], what: str) -> "array.array":
    """Run ffmpeg with raw s16le on stdout and return it as ``array('h')``.

    Binary stdout, so this is the one place that calls subprocess directly
    instead of ``ffmpeg.run()`` (which is text-mode). Still an argv list, still
    ``shell=False`` -- CONTRACT section 0.5 is about the shell, not the helper.
    """
    proc = subprocess.run(
        argv, capture_output=True, stdin=subprocess.DEVNULL, shell=False, check=False
    )
    if proc.returncode != 0:
        raise AudioError(
            f"could not decode {what}: "
            + (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
        )
    data = proc.stdout
    samples = array.array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder == "big":
        # The stream is s16LE by request; array('h') is host order.
        samples.byteswap()
    return samples


def _mono_prefix(sample_rate: int) -> str:
    # aresample BEFORE atrim so start_sample counts in TARGET-rate samples.
    return f"[0:a]aformat=channel_layouts=mono,aresample={sample_rate}"


def decode_pcm_all(path: str, *, sample_rate: int = 48000) -> "array.array":
    """Decode a whole audio track to mono s16. Sample-exact by construction.

    Measured on this machine: 37 ms for 30 s of 48 kHz PCM in Matroska, so a
    20-minute source costs ~1.5 s and ~57 MB as s16 mono. That is the right
    trade for anything that needs many windows out of one file -- one decode
    beats 180 of them.
    """
    argv = [
        FFMPEG, "-v", "error", "-i", path, "-vn",
        "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", "-f", "s16le", "-",
    ]
    return _decode_s16(argv, path)


def decode_pcm_window(
    path: str,
    start_sample: int,
    num_samples: int,
    *,
    sample_rate: int = 48000,
) -> "array.array":
    """Decode a mono s16 window as a stdlib ``array('h')``. Sample-EXACT.

    Uses ``atrim=start_sample=/end_sample=``, which counts decoded samples, and
    does NOT seek. Seeking was tried and is not trustworthy here: measured on
    the same Matroska source, ``-ss 9`` landed exactly on sample 480000 while
    ``-ss 8`` landed ~8 samples late, because MKV timestamps are quantized to
    1 ms and the accurate-seek trim inherits that granularity. A caller reading
    a +/-240-sample window has no way to tell an 8-sample bias from a real
    result, and a zero-crossing snap that is 8 samples off is the click it was
    supposed to remove.

    The cost is decoding from the head of the file (~1.5 s on a 20-minute
    source). For many windows out of one file use ``decode_pcm_all`` once --
    which is what ``apply_zero_cross_snap`` does.
    """
    if num_samples <= 0:
        return array.array("h")
    start_sample = max(0, start_sample)
    end_sample = start_sample + num_samples
    graph = (
        f"{_mono_prefix(sample_rate)},"
        f"atrim=start_sample={start_sample}:end_sample={end_sample}[o]\n"
    )
    fc = write_filtergraph(graph, os.path.join(default_cache_root(), "fc"))
    argv = (
        [FFMPEG, "-v", "error", "-i", path, "-vn"]
        + filter_complex_args(fc)
        + ["-map", "[o]", "-c:a", "pcm_s16le", "-f", "s16le", "-"]
    )
    return _decode_s16(argv, f"{path} at sample {start_sample}")


def snap_zero_crossing(
    path: str,
    sample_index: int,
    *,
    sample_rate: int = 48000,
    search_ms: float = ZERO_CROSS_SEARCH_MS,
    prefer_slope: int | None = None,
) -> ZeroCross:
    """Find a zero crossing within +/-``search_ms`` of ``sample_index``.

    Why this exists: WhisperX gives the boundary of a *phonetic* word, not of a
    silent sample. Splicing sample x onto sample y creates a near-instantaneous
    step whose spectrum is broadband -- the click. Measured on this machine, a
    mid-phase hard cut produced a 3508 LSB sample-to-sample step against a 119
    LSB baseline (29.5x); snapping to a zero crossing brought it back to exactly
    119 LSB. Room tone is never zero, so "it was silent there" does not help.

    Returns the DELTA to add to the cut point, in samples. It is a sub-frame,
    audio-only adjustment and must never move the video cut (CONTRACT
    section 8) -- which is why it lands in ``audio.zero_cross_offset_samples``
    and nowhere else.

    ``prefer_slope`` (+1 rising, -1 falling) restricts the search to crossings
    with that slope sign, which is how both sides of one splice are made to meet
    with the waveform travelling the same direction.
    """
    radius = max(1, int(search_ms / 1000.0 * sample_rate + 0.5))
    # One extra sample each side: a crossing is a property of a PAIR.
    window_start = max(0, sample_index - radius - 1)
    window_len = (sample_index + radius + 2) - window_start
    pcm = decode_pcm_window(path, window_start, window_len, sample_rate=sample_rate)
    return find_zero_crossing(pcm, sample_index - window_start, radius, prefer_slope)


def find_zero_crossing(
    pcm: "array.array",
    centre: int,
    radius: int,
    prefer_slope: int | None = None,
) -> ZeroCross:
    """The search itself, over an in-memory buffer. No I/O.

    Split out so ``apply_zero_cross_snap`` can decode a source once and search
    it many times, and so the search is testable without ffmpeg.
    """
    if len(pcm) < 2:
        return ZeroCross(0, 0, False, radius)
    # Walk outward so the nearest crossing wins; ties resolve to the later
    # sample, matching the round-half-up convention used for frame INs.
    for step in range(0, radius + 1):
        for delta in ((0,) if step == 0 else (step, -step)):
            i = centre + delta
            if i <= 0 or i >= len(pcm):
                continue
            prev, cur = pcm[i - 1], pcm[i]
            if prev == 0 and cur == 0:
                continue
            crosses = (prev <= 0 < cur) or (prev >= 0 > cur) or cur == 0
            if not crosses:
                continue
            slope = 1 if cur > prev else (-1 if cur < prev else 0)
            if prefer_slope is not None and slope != prefer_slope:
                continue
            return ZeroCross(delta, slope, True, radius)
    return ZeroCross(0, 0, False, radius)


def snap_splice(
    out_path: str,
    out_sample: int,
    in_path: str,
    in_sample: int,
    *,
    sample_rate: int = 48000,
    search_ms: float = ZERO_CROSS_SEARCH_MS,
) -> tuple[int, int]:
    """Snap BOTH sides of one splice to a zero crossing of the SAME slope.

    Two independent snaps can land on opposite slopes and reintroduce the step
    they were supposed to remove. This tries rising for both, then falling, and
    only falls back to independent snapping if neither side has both.
    """
    for slope in (1, -1):
        a = snap_zero_crossing(
            out_path, out_sample, sample_rate=sample_rate, search_ms=search_ms, prefer_slope=slope
        )
        b = snap_zero_crossing(
            in_path, in_sample, sample_rate=sample_rate, search_ms=search_ms, prefer_slope=slope
        )
        if a.found and b.found:
            return a.delta_samples, b.delta_samples
    a = snap_zero_crossing(out_path, out_sample, sample_rate=sample_rate, search_ms=search_ms)
    b = snap_zero_crossing(in_path, in_sample, sample_rate=sample_rate, search_ms=search_ms)
    return a.delta_samples, b.delta_samples


def slope_at_index(pcm: "array.array", index: int) -> int:
    """Sign of the waveform's slope at one sample: +1 rising, -1 falling, 0 flat."""
    if index <= 0 or index >= len(pcm):
        return 0
    delta = pcm[index] - pcm[index - 1]
    return 1 if delta > 0 else (-1 if delta < 0 else 0)


def slope_at(path: str, sample_index: int, *, sample_rate: int = 48000) -> int:
    """``slope_at_index`` for callers that only have a path. One decode."""
    pcm = decode_pcm_window(path, max(0, sample_index - 1), 2, sample_rate=sample_rate)
    return slope_at_index(pcm, 1)


def apply_zero_cross_snap(project: Project, *, search_ms: float = ZERO_CROSS_SEARCH_MS) -> int:
    """Fill in ``zero_cross_offset_samples`` on every enabled segment.

    Schema v1.1 gives a segment ONE offset, and it shifts the whole audio window
    (both ends move together). So this snaps each segment's audio IN point -- the
    audible side of the splice -- and steers the choice with the slope the
    PREVIOUS segment's OUT point ended on, so the waveform crosses zero
    travelling the same direction on both sides of the join. The 20 ms afade on
    each end is the second half of the belt-and-braces: measured, either one
    alone brings a mid-phase splice's 3508 LSB step back to the 119 LSB baseline.

    Exactly one write per segment, so re-running it is a no-op and returns 0.
    That matters: this runs before every audio pass and a non-idempotent version
    would make the render depend on how many times it had been called.
    """
    tb = project.timebase()
    radius = max(1, int(search_ms / 1000.0 * tb.sample_rate + 0.5))
    changed = 0
    prev_slope: int | None = None

    # One decode per SOURCE, not per segment: 180 segments off one camera would
    # otherwise be 180 full decodes, and decode_pcm_window does not seek.
    decoded: dict[str, array.array] = {}

    for seg in project.enabled_segments():
        path = project.resolve(str(project.source(str(seg["source_id"]))["path"]))
        if path not in decoded:
            decoded[path] = decode_pcm_all(path, sample_rate=tb.sample_rate)
        pcm = decoded[path]

        a_in, a_out, _lead, _tail = _audio_source_range(seg)
        in_sample = tb.frames_to_samples(max(0, a_in))

        zc = find_zero_crossing(pcm, in_sample, radius, prev_slope)
        if not zc.found and prev_slope is not None:
            # No matching-slope crossing in range. A crossing on the wrong slope
            # still beats splicing mid-phase, so take it.
            zc = find_zero_crossing(pcm, in_sample, radius, None)

        delta = zc.delta_samples
        block = seg.setdefault("audio", {})
        if int(block.get("zero_cross_offset_samples", 0) or 0) != delta:
            block["zero_cross_offset_samples"] = delta
            changed += 1

        out_sample = tb.frames_to_samples(a_out) + delta
        prev_slope = slope_at_index(pcm, out_sample) or None

    return changed


__all__ = [
    "AudioError",
    "AudioPlan",
    "ZeroCross",
    "DEFAULT_FADE_MS",
    "ZERO_CROSS_SEARCH_MS",
    "build_audio_graph",
    "build_audio_plan",
    "render_plan_graph",
    "render_audio",
    "loudnorm_two_pass",
    "decode_pcm_all",
    "decode_pcm_window",
    "find_zero_crossing",
    "snap_zero_crossing",
    "snap_splice",
    "slope_at",
    "slope_at_index",
    "apply_zero_cross_snap",
]
