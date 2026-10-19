"""ffprobe wrappers, normalized so nothing downstream parses ffprobe output twice.

Two facts drive the shape of this module:

- ``nb_frames`` is absent or wrong in MKV and unreliable everywhere. The only
  authoritative count is ``-count_frames`` (it decodes; it is slow; it is right).
- The concat demuxer does NOT check that its inputs match. On a parameter
  mismatch it writes a corrupt file and exits 0. ``concat_compatible()`` must be
  called before every ``-c copy`` join.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .ffmpeg import FFPROBE, VeditError, ffprobe_json, run


class ProbeError(VeditError):
    """ffprobe could not describe the file."""


_VIDEO_CONCAT_FIELDS = ("v_codec", "width", "height", "pix_fmt", "fps_num", "fps_den")
_AUDIO_CONCAT_FIELDS = ("a_codec", "sample_rate", "channels", "channel_layout")


def _parse_rate(text: str | None) -> tuple[int, int]:
    """ffprobe rate strings are ``num/den``; ``0/0`` means "no idea"."""
    if not text or "/" not in text:
        return (0, 1)
    num, den = text.split("/", 1)
    try:
        n, d = int(num), int(den)
    except ValueError:
        return (0, 1)
    if d == 0:
        return (0, 1)
    frac = Fraction(n, d)
    return (frac.numerator, frac.denominator)


def probe(path: str) -> dict[str, Any]:
    """Normalized description of a media file.

    Returns a dict with stable keys regardless of container: duration_s,
    fps_num, fps_den, width, height, pix_fmt, v_codec, a_codec, sample_rate,
    channels, channel_layout, is_vfr, has_video, has_audio, nb_frames_declared.
    """
    data = ffprobe_json(
        [
            "-show_streams",
            "-show_format",
            "-i",
            path,
        ]
    )
    streams = data.get("streams") or []
    if not streams:
        raise ProbeError(f"no streams in {path}")
    fmt = data.get("format") or {}

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_s = 0.0
    for candidate in (fmt.get("duration"), (v or {}).get("duration"), (a or {}).get("duration")):
        try:
            if candidate is not None:
                duration_s = float(candidate)
                break
        except (TypeError, ValueError):
            continue

    r_num, r_den = _parse_rate((v or {}).get("r_frame_rate"))
    avg_num, avg_den = _parse_rate((v or {}).get("avg_frame_rate"))

    nb_declared: int | None = None
    if v is not None and v.get("nb_frames") not in (None, "N/A"):
        try:
            nb_declared = int(v["nb_frames"])
        except (TypeError, ValueError):
            nb_declared = None

    out: dict[str, Any] = {
        "path": path,
        "duration_s": duration_s,
        "has_video": v is not None,
        "has_audio": a is not None,
        "fps_num": r_num,
        "fps_den": r_den,
        "avg_fps_num": avg_num,
        "avg_fps_den": avg_den,
        "width": int(v["width"]) if v and v.get("width") is not None else 0,
        "height": int(v["height"]) if v and v.get("height") is not None else 0,
        "pix_fmt": (v or {}).get("pix_fmt"),
        "v_codec": (v or {}).get("codec_name"),
        "nb_frames_declared": nb_declared,
        "a_codec": (a or {}).get("codec_name"),
        "sample_rate": int(a["sample_rate"]) if a and a.get("sample_rate") not in (None, "N/A") else 0,
        "channels": int(a["channels"]) if a and a.get("channels") is not None else 0,
        "channel_layout": (a or {}).get("channel_layout"),
    }
    # Cheap verdict only: no second ffprobe. probe() is called once per file by
    # concat_compatible() on every segment, so an extra process spawn here costs
    # hundreds of spawns on a real timeline. is_vfr() is the deep check.
    out["is_vfr"] = _rates_disagree(out)
    return out


def is_vfr(path: str, *, sample_packets: int = 300) -> bool:
    """Authoritative VFR verdict. Use this at intake.

    VFR intake is untenable here: ffprobe and other tools disagree on the frame
    count of the same VFR mp4 (measured 113 vs 111), so intake mandatorily
    transcodes to the project's CFR rate. This is the gate that catches it.

    Stricter than ``probe()["is_vfr"]``: matching r_frame_rate and
    avg_frame_rate are necessary but not sufficient -- a container can average
    out to a clean rate while individual packet durations vary -- so this also
    samples real packet durations.
    """
    info = probe(path)
    if not info["has_video"]:
        return False
    if _rates_disagree(info):
        return True
    return _packets_vary(path, sample_packets)


def _rates_disagree(info: dict[str, Any]) -> bool:
    if not info.get("has_video"):
        return False
    r = Fraction(info["fps_num"], info["fps_den"]) if info["fps_num"] else Fraction(0)
    avg = Fraction(info["avg_fps_num"], info["avg_fps_den"]) if info["avg_fps_num"] else Fraction(0)
    if not r or not avg:
        return False
    return abs(float(r - avg)) / float(r) > 0.001


def _packets_vary(path: str, sample_packets: int) -> bool:
    if sample_packets <= 0:
        return False
    try:
        data = ffprobe_json(
            [
                "-select_streams",
                "v:0",
                "-show_entries",
                "packet=duration",
                "-read_intervals",
                f"%+#{sample_packets}",
                "-i",
                path,
            ]
        )
    except (VeditError, ValueError):
        return False
    durations = []
    for pkt in data.get("packets") or []:
        d = pkt.get("duration")
        if d in (None, "N/A"):
            continue
        try:
            durations.append(int(d))
        except (TypeError, ValueError):
            continue
    if len(durations) < 3:
        return False
    # Drop the last packet: a trailing short packet is normal at EOF.
    body = durations[:-1]
    return len(set(body)) > 1


def count_frames(path: str) -> int:
    """Authoritative decoded video frame count.

    ``-count_frames`` decodes the whole track. It is the only number QC check (a)
    may be compared against; ``nb_frames`` from the container is a hint at best.
    """
    proc = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
            "-i",
            path,
        ]
    )
    text = (proc.stdout or "").strip().splitlines()
    for line in text:
        line = line.strip().rstrip(",")
        if line.isdigit():
            return int(line)
    raise ProbeError(f"could not count frames in {path}: {proc.stdout!r}")


def count_audio_samples(path: str) -> int:
    """Decoded audio sample count per channel. Used by QC check (b)."""
    info = probe(path)
    if not info["has_audio"]:
        return 0
    proc = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-count_frames",
            "-show_entries",
            "stream=duration_ts,time_base",
            "-of",
            "json",
            "-i",
            path,
        ]
    )
    import json

    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    ts = stream.get("duration_ts")
    tb_num, tb_den = _parse_rate(stream.get("time_base"))
    if ts is None or not tb_den:
        return 0
    # time_base for audio is normally 1/sample_rate, so duration_ts IS samples.
    return int(Fraction(int(ts) * tb_num * info["sample_rate"], tb_den))


def concat_compatible(paths: list[str]) -> tuple[bool, str]:
    """Guard before ``-f concat -safe 0 -c copy``.

    The demuxer does not validate its inputs. Feed it two files whose codec,
    resolution, pix_fmt, frame rate or audio layout differ and it produces a
    corrupt output file and exits 0. Returns (ok, reason).
    """
    if not paths:
        return False, "no inputs"
    if len(paths) == 1:
        return True, "single input"

    infos = []
    for p in paths:
        try:
            infos.append((p, probe(p)))
        except (VeditError, ValueError) as exc:
            return False, f"{p}: unprobeable ({exc})"

    ref_path, ref = infos[0]
    for path, info in infos[1:]:
        if info["has_video"] != ref["has_video"] or info["has_audio"] != ref["has_audio"]:
            return False, (
                f"stream layout differs: {ref_path} has "
                f"video={ref['has_video']}/audio={ref['has_audio']}, "
                f"{path} has video={info['has_video']}/audio={info['has_audio']}"
            )
        if ref["has_video"]:
            for field in _VIDEO_CONCAT_FIELDS:
                if info[field] != ref[field]:
                    return False, (
                        f"video {field} differs: {ref_path}={ref[field]!r} vs {path}={info[field]!r}"
                    )
        if ref["has_audio"]:
            for field in _AUDIO_CONCAT_FIELDS:
                if info[field] != ref[field]:
                    return False, (
                        f"audio {field} differs: {ref_path}={ref[field]!r} vs {path}={info[field]!r}"
                    )
    return True, f"{len(paths)} inputs match on codec/geometry/rate"
