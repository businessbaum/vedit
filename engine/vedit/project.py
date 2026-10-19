"""``project.json`` -- the edit decision document, schema v1.1.

Two invariants this module enforces mechanically (CONTRACT section 0):

- **Timeline positions are DERIVED.** ``_computed.positions`` is a running sum
  over enabled segments sorted by ``order``, rewritten on every load. Nothing
  hand-edits it and nothing stores it as authority.
- **Nothing is ever deleted.** Rejected material is ``"enabled": false`` with a
  reason and provenance, so every automatic decision is one boolean away from
  being reversed.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from typing import Any, Iterator

from .ffmpeg import VeditError
from .timebase import Timebase

SCHEMA_VERSION = "1.1"

_STAGES = (
    "intake",
    "transcribed",
    "story",
    "rough_cut",
    "graphics",
    "captions",
    "music",
    "export",
)

_COMPUTED_NOTE = "DERIVED - do not hand-edit. Rewritten on every load."


class ProjectError(VeditError):
    """project.json is missing, unreadable, or not a shape we understand."""


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _skeleton(slug: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": slug,
        "created_utc": _utc_now(),
        "stage": "intake",
        "format_preset": "longform",
        "output": {
            "platform": "youtube",
            "path": "out/final.mp4",
            "resolution": [1920, 1080],
            "v_codec": "h264_nvenc",
            "cq": 20,
            "preset": "p6",
            "a_codec": "aac",
            "a_bitrate": "320k",
            "loudnorm": {"I": -14, "TP": -1.5, "LRA": 11},
        },
        "timebase": {"fps_num": 30, "fps_den": 1, "sample_rate": 48000},
        # Every threshold in presets/ is research-derived, not perceptually
        # tested. QC warns while this is false.
        "calibrated": False,
        "sources": [],
        "transcript": None,
        "story": {"blocks": [], "hook": None},
        "segments": [],
        "overlays": [],
        "captions": {
            "enabled": False,
            "burn_in": True,
            "preset": "shortform-highlight",
            "sidecar_path": "out/captions.ass",
            "overrides": [],
        },
        "audio_tracks": [{"id": "vo", "kind": "dialogue", "source": "segments", "gain_db": 0.0}],
        "_computed": {},
    }


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay wins, dicts merge, lists replace wholesale.

    Lists replace because merging them element-wise would silently reorder
    segments, and order is authority here.
    """
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Project:
    """A loaded project.json plus the directory it resolves paths against."""

    def __init__(self, data: dict[str, Any], path: str) -> None:
        self.data = data
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)

    # --- construction ---------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> "Project":
        """Read project.json and recompute the derived block before returning."""
        p = os.path.abspath(path)
        if os.path.isdir(p):
            p = os.path.join(p, "project.json")
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError as exc:
            raise ProjectError(f"no project at {p}") from exc
        except ValueError as exc:
            raise ProjectError(f"{p} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectError(f"{p} must contain a JSON object")
        version = str(data.get("schema_version", ""))
        if version != SCHEMA_VERSION:
            raise ProjectError(
                f"{p} is schema_version {version!r}, this engine speaks {SCHEMA_VERSION!r}"
            )
        proj = cls(data, p)
        proj.recompute()
        return proj

    @classmethod
    def create(cls, root: str, slug: str, preset: dict) -> "Project":
        """Scaffold ``<root>/projects/<slug>/`` and write a fresh project.json.

        ``preset`` is a format preset (presets/formats/*.json) deep-merged over
        the skeleton, so a preset only has to name what it changes.
        """
        if not slug or any(c in slug for c in '\\/:*?"<>|'):
            raise ProjectError(f"invalid project slug {slug!r}")
        proj_dir = os.path.join(os.path.abspath(root), "projects", slug)
        path = os.path.join(proj_dir, "project.json")
        if os.path.exists(path):
            raise ProjectError(f"project already exists: {path}")
        for sub in ("media", "out"):
            os.makedirs(os.path.join(proj_dir, sub), exist_ok=True)

        data = _skeleton(slug)
        if preset:
            merged = dict(preset)
            # A preset may not override identity or the derived block.
            for reserved in ("project_id", "created_utc", "_computed", "schema_version"):
                merged.pop(reserved, None)
            data = _deep_merge(data, merged)
            if "name" in merged and "format_preset" not in merged:
                data["format_preset"] = merged["name"]
        proj = cls(data, path)
        proj.recompute()
        proj.save()
        return proj

    # --- persistence ----------------------------------------------------------

    def save(self) -> None:
        """Atomic write. A half-written project.json is an unrecoverable edit."""
        self.recompute()
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="project.", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- derived state --------------------------------------------------------

    def timebase(self) -> Timebase:
        tb = self.data.get("timebase") or {}
        try:
            return Timebase.from_dict(tb)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectError(f"bad timebase {tb!r}: {exc}") from exc

    def enabled_segments(self) -> list[dict]:
        """Enabled segments in output order.

        Sorted by ``order``, tie-broken by position in the file so the result is
        deterministic even in a half-edited document.
        """
        segs = self.data.get("segments") or []
        indexed = [(s, i) for i, s in enumerate(segs) if s.get("enabled", True)]
        indexed.sort(key=lambda pair: (_order_of(pair[0], pair[1]), pair[1]))
        return [s for s, _ in indexed]

    @staticmethod
    def segment_frames(seg: dict) -> int:
        """Timeline length of one segment, in frames.

        Normally ``src_out - src_in`` exactly (QC check (a) compares against that
        sum). ``speed`` is applied only when it is not 1.0, because a retimed
        segment genuinely occupies a different number of timeline frames and
        ignoring that would put every later segment at the wrong position.
        """
        src = int(seg["src_out_frames"]) - int(seg["src_in_frames"])
        speed = float(seg.get("speed", 1.0) or 1.0)
        if speed == 1.0:
            return src
        return max(1, int(src / speed + 0.5))

    def recompute(self) -> None:
        """Rebuild ``_computed`` from authority. Idempotent.

        ``generated_utc`` is only refreshed when a derived value actually
        changed, so calling recompute() twice produces a byte-identical document
        and save() does not churn the file on every load.
        """
        tb = self.timebase()
        positions: dict[str, dict[str, int]] = {}
        cursor = 0
        segments = self.enabled_segments()
        for seg in segments:
            try:
                length = self.segment_frames(seg)
            except (KeyError, TypeError, ValueError) as exc:
                raise ProjectError(f"segment {seg.get('id')!r} has bad frame bounds: {exc}") from exc
            if length <= 0:
                raise ProjectError(
                    f"segment {seg.get('id')!r} is empty: "
                    f"{seg.get('src_in_frames')} -> {seg.get('src_out_frames')}"
                )
            positions[str(seg.get("id"))] = {"tl_in": cursor, "tl_out": cursor + length}
            cursor += length

        derived = {
            "note": _COMPUTED_NOTE,
            "total_frames": cursor,
            "total_seconds": round(tb.frames_to_seconds(cursor), 6),
            "total_samples": tb.frames_to_samples(cursor),
            "enabled_segment_count": len(segments),
            "positions": positions,
        }

        previous = self.data.get("_computed") or {}
        stale = {k: v for k, v in previous.items() if k != "generated_utc"}
        if stale != derived:
            derived["generated_utc"] = _utc_now()
        else:
            derived["generated_utc"] = previous.get("generated_utc", _utc_now())
        self.data["_computed"] = derived

    # --- lookups --------------------------------------------------------------

    def resolve(self, rel: str) -> str:
        """Project-relative path -> absolute path.

        Every path in project.json is relative to the project directory, which is
        what lets a project be moved or copied without rewriting it. Absolute
        input is passed through so ``original_path`` still works.
        """
        if not rel:
            raise ProjectError("cannot resolve an empty path")
        if os.path.isabs(rel):
            return os.path.normpath(rel)
        return os.path.normpath(os.path.join(self.dir, rel))

    def source(self, source_id: str) -> dict:
        for src in self.data.get("sources") or []:
            if src.get("id") == source_id:
                return src
        raise ProjectError(f"no source {source_id!r} in {self.path}")

    def segment(self, segment_id: str) -> dict:
        for seg in self.data.get("segments") or []:
            if seg.get("id") == segment_id:
                return seg
        raise ProjectError(f"no segment {segment_id!r} in {self.path}")

    def position(self, segment_id: str) -> dict[str, int]:
        """Timeline in/out of an enabled segment, from the derived block."""
        pos = (self.data.get("_computed") or {}).get("positions") or {}
        if segment_id not in pos:
            raise ProjectError(f"segment {segment_id!r} is not on the timeline (disabled or unknown)")
        return pos[segment_id]

    @property
    def total_frames(self) -> int:
        return int((self.data.get("_computed") or {}).get("total_frames", 0))

    @property
    def stage(self) -> str:
        return str(self.data.get("stage", "intake"))

    def set_stage(self, stage: str) -> None:
        if stage not in _STAGES:
            raise ProjectError(f"unknown stage {stage!r}, expected one of {_STAGES}")
        self.data["stage"] = stage

    # --- dict-ish sugar -------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __repr__(self) -> str:
        return (
            f"<Project {self.data.get('project_id')!r} stage={self.stage!r} "
            f"segments={len(self.data.get('segments') or [])} frames={self.total_frames}>"
        )


def _order_of(seg: dict, fallback: int) -> int:
    value = seg.get("order")
    if value is None:
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
