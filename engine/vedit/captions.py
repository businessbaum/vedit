"""Captions: word timings -> canonical ASS -> burned-in pixels.

Three things in here are load-bearing and were established the hard way
(CONTRACT section 7):

1. **The Format lines.** libass parses ``[V4+ Styles]`` positionally against its
   own field list. A Format line that does not match makes libass exit 0, drop
   Outline and Bold, and prepend a stray comma into the rendered text. So the
   Format lines are constants in this module, not something a caller composes,
   and validate_ass() refuses to hand a malformed file to the burn-in.

2. **Fontname must be the font file's nameID 1 string, and a weight that is
   already part of the family name must NOT also set Bold.** ``Inter
   ExtraBold`` + ``Bold: -1`` asks libass for a bold of a bold; it finds no such
   face, silently falls back to DirectWrite/Arial, and logs nothing. FONT_TABLE
   below is the verified mapping; check_font_table() re-derives it from the
   actual files so a font swap cannot rot it unnoticed.

3. **Grouping is a PIXEL budget, never max_chars.** German compound nouns are
   single unsplittable tokens: ``Geschwindigkeitsbegrenzung`` is 26 characters
   and 1346 px wide in Inter ExtraBold at 90 px -- 70 % of a 1920 frame, in ONE
   token. A char cap either wraps mid-word (which changes how a compound reads)
   or overflows the frame. estimate_text_width_px() therefore reads hmtx/cmap
   out of the real bundled TTF, so the budget is measured rather than guessed.

Preset files in ``presets/captions/*.json`` use the richer "v1" document schema
(font/ass_style/grouping/overflow/highlight/position blocks). load_preset()
translates that into the flat internal shape used here; both are accepted.
"""

from __future__ import annotations

import functools
import json

import os
import struct
from typing import Any, Iterable, Sequence

from .ffmpeg import FFMPEG, escape_filter_path, run
from .keys import repo_root
from .timebase import Timebase

# --- the canonical Format lines ----------------------------------------------
#
# 23 fields for V4+ Styles, 10 for Events -- libass's own field lists (ass.c
# style_fields / event_fields). "Canonical" means "byte-identical to what libass
# parses", and libass 8.1.1 parses 23 style fields; a 24th column shifts
# Encoding out of every row. See the build report for the measurement.

STYLE_FORMAT: tuple[str, ...] = (
    "Name",
    "Fontname",
    "Fontsize",
    "PrimaryColour",
    "SecondaryColour",
    "OutlineColour",
    "BackColour",
    "Bold",
    "Italic",
    "Underline",
    "StrikeOut",
    "ScaleX",
    "ScaleY",
    "Spacing",
    "Angle",
    "BorderStyle",
    "Outline",
    "Shadow",
    "Alignment",
    "MarginL",
    "MarginR",
    "MarginV",
    "Encoding",
)

EVENT_FORMAT: tuple[str, ...] = (
    "Layer",
    "Start",
    "End",
    "Style",
    "Name",
    "MarginL",
    "MarginR",
    "MarginV",
    "Effect",
    "Text",
)

STYLE_NAME = "Default"

# --- fonts --------------------------------------------------------------------
#
# (ASS Fontname, ASS Bold) -> filename in assets/fonts. Read out of the files'
# name tables; check_font_table() proves it still holds. Every entry whose
# weight is spelled in the family name carries Bold: 0 -- see (2) above.

FONT_TABLE: dict[tuple[str, int], str] = {
    ("Inter", 0): "Inter-Regular.ttf",
    ("Inter", -1): "Inter-Bold.ttf",
    ("Inter SemiBold", 0): "Inter-SemiBold.ttf",
    ("Inter ExtraBold", 0): "Inter-ExtraBold.ttf",
    ("Montserrat", -1): "Montserrat-Bold.ttf",
    ("Montserrat ExtraBold", 0): "Montserrat-ExtraBold.ttf",
}


def fonts_dir() -> str:
    """The one directory libass is pointed at. Contains fonts and nothing else.

    libass opens EVERY file it finds here; a stray README makes it log
    ``Error opening memory font`` once per render.
    """
    return os.path.join(repo_root(), "assets", "fonts")


def font_path(family: str, bold: int = 0, *, directory: str | None = None) -> str:
    """Resolve an ASS (Fontname, Bold) pair to the static TTF that backs it."""
    d = directory or fonts_dir()
    name = FONT_TABLE.get((family, int(bold)))
    if name is None:
        # Unknown family: measure with the regular of the first family word so a
        # typo produces a slightly wrong width rather than a crash mid-render.
        name = FONT_TABLE.get((family.split(" ")[0], 0), "Inter-Regular.ttf")
    return os.path.join(d, name)


# --- TTF metrics (stdlib only) ------------------------------------------------
#
# Everything below parses the sfnt tables directly. There is no font library on
# the stdlib-only side of this pipeline, and guessing an average glyph width is
# exactly the failure the pixel budget exists to remove.
#
# Kerning is NOT applied: Inter and Montserrat carry kerning in GPOS, a far
# larger parse than hmtx. The omission over-estimates width slightly, which is
# the safe direction to be wrong about a budget.


class FontMetricsError(Exception):
    """The font file could not be parsed well enough to measure text."""


class FontMetrics:
    """Advance widths for one static TTF, in font units and in pixels."""

    __slots__ = (
        "path",
        "family",
        "subfamily",
        "units_per_em",
        "size_units",
        "_advances",
        "_cmap",
        "_default",
    )

    def __init__(self, path: str) -> None:
        with open(path, "rb") as fh:
            data = fh.read()
        self.path = path
        tables = _sfnt_tables(data)
        self.units_per_em = _read_head(data, tables)
        self.size_units = _read_size_units(data, tables, self.units_per_em)
        self._advances = _read_hmtx(data, tables)
        self._cmap = _read_cmap(data, tables)
        self.family, self.subfamily = _read_names(data, tables)
        # Glyph 0 is .notdef; its advance is the honest width of "cannot draw
        # this", which is also what libass will lay out.
        self._default = self._advances[0] if self._advances else 0

    def glyph_for(self, ch: str) -> int:
        return self._cmap.get(ord(ch), 0)

    def advance_units(self, ch: str) -> int:
        gid = self._cmap.get(ord(ch))
        if gid is None:
            return self._default
        if gid < len(self._advances):
            return self._advances[gid]
        # Past numberOfHMetrics every glyph shares the last advance (hmtx spec).
        return self._advances[-1] if self._advances else self._default

    def text_width_units(self, text: str) -> int:
        return sum(self.advance_units(c) for c in text)

    def text_width_px(self, text: str, font_px: float, *, spacing_px: float = 0.0) -> float:
        """Advance width in pixels at the given ASS Fontsize.

        ASS Fontsize is NOT an em size. libass keeps VSFilter's convention:
        Fontsize is the font HEIGHT, usWinAscent + usWinDescent, so px-per-unit
        is font_px / size_units, not font_px / unitsPerEm.

        Measured against libass 8.1.1 by burning single lines onto black and
        scanning the columns. Using unitsPerEm over-estimates by 21 % on Inter
        (2478 vs 2048) and by 56 % on Montserrat (1562 vs 1000) -- enough to
        wrap every second line one word early and shrink cues that never needed
        it. With size_units the residual error is under 3 % and negative, which
        is the difference between advance width and inked extent.
        """
        if not text:
            return 0.0
        w = self.text_width_units(text) * (float(font_px) / float(self.size_units))
        if spacing_px:
            w += spacing_px * len(text)  # ASS Spacing adds after every glyph
        return w

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FontMetrics {self.family!r} {self.subfamily!r} upem={self.units_per_em}>"


def _sfnt_tables(data: bytes) -> dict[str, tuple[int, int]]:
    if len(data) < 12:
        raise FontMetricsError("file too short to be a font")
    if data[:4] == b"ttcf":
        (offset,) = struct.unpack_from(">I", data, 12)
        return _sfnt_tables_at(data, offset)
    return _sfnt_tables_at(data, 0)


def _sfnt_tables_at(data: bytes, base: int) -> dict[str, tuple[int, int]]:
    version, num_tables = struct.unpack_from(">IH", data, base)
    if version not in (0x00010000, 0x4F54544F, 0x74727565):
        raise FontMetricsError(f"unsupported sfnt version 0x{version:08x}")
    out: dict[str, tuple[int, int]] = {}
    for i in range(num_tables):
        off = base + 12 + i * 16
        tag, _checksum, toff, tlen = struct.unpack_from(">4sIII", data, off)
        out[tag.decode("latin-1")] = (toff, tlen)
    return out


def _read_head(data: bytes, tables: dict[str, tuple[int, int]]) -> int:
    if "head" not in tables:
        raise FontMetricsError("no head table")
    (upem,) = struct.unpack_from(">H", data, tables["head"][0] + 18)
    if upem == 0:
        raise FontMetricsError("unitsPerEm is 0")
    return upem


def _read_size_units(data: bytes, tables: dict[str, tuple[int, int]], upem: int) -> int:
    """Font units that one ASS ``Fontsize`` pixel-height covers.

    OS/2 usWinAscent + usWinDescent, which is what VSFilter used and what libass
    reproduces. Falls back to unitsPerEm when there is no OS/2 table, which is
    the pre-scale behaviour and only wrong for fonts that have no OS/2 anyway.
    """
    entry = tables.get("OS/2")
    if entry is None or entry[1] < 78:
        return upem
    win_asc, win_desc = struct.unpack_from(">HH", data, entry[0] + 74)
    total = win_asc + win_desc
    return total if total > 0 else upem


def _read_hmtx(data: bytes, tables: dict[str, tuple[int, int]]) -> list[int]:
    if "hhea" not in tables or "hmtx" not in tables:
        raise FontMetricsError("no hhea/hmtx")
    (num_h_metrics,) = struct.unpack_from(">H", data, tables["hhea"][0] + 34)
    hmtx = tables["hmtx"][0]
    return [struct.unpack_from(">H", data, hmtx + i * 4)[0] for i in range(num_h_metrics)]


def _read_cmap(data: bytes, tables: dict[str, tuple[int, int]]) -> dict[int, int]:
    if "cmap" not in tables:
        raise FontMetricsError("no cmap table")
    base = tables["cmap"][0]
    _version, num_subtables = struct.unpack_from(">HH", data, base)
    # Preference: full Unicode (3,10), then BMP (3,1), then the Unicode platform.
    best_off = -1
    best_rank = -1
    for i in range(num_subtables):
        plat, enc, off = struct.unpack_from(">HHI", data, base + 4 + i * 8)
        rank = {(3, 10): 3, (3, 1): 2, (0, 4): 2, (0, 6): 1, (0, 3): 1}.get((plat, enc), 0)
        if rank > best_rank:
            best_rank, best_off = rank, base + off
    if best_off < 0:
        raise FontMetricsError("cmap has no subtables")
    (fmt,) = struct.unpack_from(">H", data, best_off)
    if fmt == 4:
        return _cmap_format4(data, best_off)
    if fmt == 12:
        return _cmap_format12(data, best_off)
    if fmt == 6:
        return _cmap_format6(data, best_off)
    raise FontMetricsError(f"unsupported cmap format {fmt}")


def _cmap_format4(data: bytes, off: int) -> dict[int, int]:
    _fmt, _length, _lang, seg_x2 = struct.unpack_from(">HHHH", data, off)
    seg = seg_x2 // 2
    ends = struct.unpack_from(f">{seg}H", data, off + 14)
    starts = struct.unpack_from(f">{seg}H", data, off + 16 + seg_x2)
    deltas = struct.unpack_from(f">{seg}h", data, off + 16 + seg_x2 * 2)
    range_off_base = off + 16 + seg_x2 * 3
    range_offs = struct.unpack_from(f">{seg}H", data, range_off_base)
    out: dict[int, int] = {}
    for i in range(seg):
        start, end = starts[i], ends[i]
        if start > end or start == 0xFFFF:
            continue
        for cp in range(start, end + 1):
            if range_offs[i] == 0:
                gid = (cp + deltas[i]) & 0xFFFF
            else:
                idx = range_off_base + i * 2 + range_offs[i] + (cp - start) * 2
                if idx + 2 > len(data):
                    continue
                (gid,) = struct.unpack_from(">H", data, idx)
                if gid:
                    gid = (gid + deltas[i]) & 0xFFFF
            if gid:
                out[cp] = gid
    return out


def _cmap_format12(data: bytes, off: int) -> dict[int, int]:
    _fmt, _res, _length, _lang, n_groups = struct.unpack_from(">HHIII", data, off)
    out: dict[int, int] = {}
    for i in range(n_groups):
        start, end, gid = struct.unpack_from(">III", data, off + 16 + i * 12)
        # Guard: an astral range would expand to millions of entries for glyphs
        # no caption will ever contain.
        if start > 0x2FFFF:
            continue
        end = min(end, 0x2FFFF)
        for cp in range(start, end + 1):
            out[cp] = gid + (cp - start)
    return out


def _cmap_format6(data: bytes, off: int) -> dict[int, int]:
    _fmt, _length, _lang, first, count = struct.unpack_from(">HHHHH", data, off)
    gids = struct.unpack_from(f">{count}H", data, off + 10)
    return {first + i: g for i, g in enumerate(gids) if g}


def _read_names(data: bytes, tables: dict[str, tuple[int, int]]) -> tuple[str, str]:
    if "name" not in tables:
        return ("", "")
    base = tables["name"][0]
    _fmt, count, string_off = struct.unpack_from(">HHH", data, base)
    picked: dict[int, str] = {}
    for i in range(count):
        plat, _enc, _lang, name_id, length, offset = struct.unpack_from(">HHHHHH", data, base + 6 + i * 12)
        if name_id not in (1, 2):
            continue
        raw = data[base + string_off + offset : base + string_off + offset + length]
        try:
            text = raw.decode("utf-16-be") if plat in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        # Windows (platform 3) records win: they are what libass matches on.
        if name_id not in picked or plat == 3:
            picked[name_id] = text
    return (picked.get(1, ""), picked.get(2, ""))


@functools.lru_cache(maxsize=16)
def load_font(path: str) -> FontMetrics:
    """Parse and memoize the metrics of one font file."""
    return FontMetrics(path)


def font_family_name(path: str) -> str:
    """nameID 1 of a font file -- the exact string an ASS ``Fontname`` needs."""
    return load_font(path).family


def check_font_table(directory: str | None = None) -> list[dict[str, Any]]:
    """Re-derive FONT_TABLE from the files. An empty list means the table holds.

    A silent Arial fallback costs a whole render and produces no log line, so
    this is the cheap half of QC check (e); the expensive half is the Arial
    control hash in qc.py.
    """
    d = directory or fonts_dir()
    findings: list[dict[str, Any]] = []
    for (family, bold), fname in sorted(FONT_TABLE.items()):
        p = os.path.join(d, fname)
        if not os.path.isfile(p):
            findings.append({"file": fname, "problem": "missing"})
            continue
        try:
            fm = load_font(p)
        except FontMetricsError as exc:
            findings.append({"file": fname, "problem": f"unparsable: {exc}"})
            continue
        if fm.family != family:
            findings.append(
                {"file": fname, "problem": "nameID1 mismatch", "expected": family, "actual": fm.family}
            )
        if any(w in family for w in ("SemiBold", "ExtraBold")) and bold != 0:
            findings.append({"file": fname, "problem": "weight is in the family name but Bold != 0"})
    return findings


def estimate_text_width_px(text: str, font_px: float, font: Any, *, spacing_px: float = 0.0) -> float:
    """Rendered advance width of ``text`` at ``font_px``, in pixels.

    ``font`` may be a FontMetrics, a path to a TTF, an ASS Fontname such as
    ``"Inter ExtraBold"``, or an ``(fontname, bold)`` pair. Measured from the
    hmtx/cmap of the actual bundled file, which is the entire point:
    ``Geschwindigkeitsbegrenzung`` has to be measured, not approximated from a
    character count.
    """
    return _as_metrics(font).text_width_px(text, font_px, spacing_px=spacing_px)


def _as_metrics(font: Any) -> FontMetrics:
    if isinstance(font, FontMetrics):
        return font
    if isinstance(font, tuple) and len(font) == 2:
        return load_font(font_path(str(font[0]), int(font[1])))
    if isinstance(font, str):
        return load_font(font if os.path.isfile(font) else font_path(font, 0))
    raise TypeError(f"cannot measure with font={font!r}")


# --- presets ------------------------------------------------------------------
#
# The flat internal shape. Every number here is a design default, never a
# calibrated one: nothing in this module has been checked against real German
# talking-head footage, so a project using these stays calibrated: false.

BASE_PRESET: dict[str, Any] = {
    "name": "builtin-default",
    "font_family": "Inter SemiBold",
    "bold": 0,
    "font_px": 56,
    "min_font_px": 34,
    "shrink_step_px": 2,
    "spacing_px": 0.0,
    "scale_x": 100,
    "scale_y": 100,
    "angle": 0,
    "italic": 0,
    "underline": 0,
    "strikeout": 0,
    "border_style": 1,  # 1 = outline + shadow, 3 = opaque box
    "outline_px": 3.0,
    "shadow_px": 1.0,
    # Colours may be "#RRGGBB" (converted, using the *_alpha values) or a raw
    # ASS "&HAABBGGRR" string straight out of a preset file.
    "primary": "#FFFFFF",
    "secondary": "#00D4FF",
    "outline_colour": "#101010",
    "back_colour": "#000000",
    "primary_alpha": 0.0,  # ASS stores TRANSPARENCY: 0.0 is fully opaque
    "outline_alpha": 0.0,
    "back_alpha": 0.39,
    "alignment": 2,  # bottom centre
    "margin_l_pct": 0.05,
    "margin_r_pct": 0.05,
    "margin_v_pct": 0.12,
    "margin_l_px": None,  # absolute wins over pct when present
    "margin_r_px": None,
    "margin_v_px": None,
    "pos": None,  # [x, y] -> emit an explicit \pos, for Alignment 5 bands
    "width_budget_px": None,
    "width_budget_pct": 0.75,
    "max_lines": 2,
    "max_words": 12,
    "max_duration_s": 5.0,
    "min_duration_s": 0.9,
    "min_gap_s": 0.0,
    "split_gap_s": 0.50,  # a spoken pause this long starts a new card
    "split_on_sentence_end": True,
    "split_on_clause_end": False,
    "orphan_min_words": 0,
    "lead_frames": 0,
    "tail_frames": 0,
    "highlight": False,
    "highlight_scale_pct": 100,
    "uppercase": False,
    "strip_terminal_punctuation": False,
    "encoding": 1,  # 1 = default; 0 (ANSI) mangles umlauts on some builds
    "play_res_x": 1920,
    "play_res_y": 1080,
    "fontsdir": None,
}


def _preset_dir() -> str:
    return os.path.join(repo_root(), "presets", "captions")


def load_preset(preset: str | dict | None) -> dict[str, Any]:
    """Resolve a preset name, a dict, or None into the flat internal preset.

    A name is looked up as ``presets/captions/<name>.json`` first, so the
    on-disk presets are authoritative and code holds only fallbacks. Both the
    flat internal shape and the "v1" document schema are accepted; the document
    schema is detected by its ``ass_style`` block.
    """
    if preset is None:
        return dict(BASE_PRESET)
    if isinstance(preset, dict):
        if "ass_style" in preset or preset.get("_schema", "").startswith("presets/captions"):
            return _from_document(preset)
        merged = dict(BASE_PRESET)
        merged.update({k: v for k, v in preset.items() if not k.startswith("_")})
        return merged
    path = os.path.join(_preset_dir(), f"{preset}.json")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            return load_preset(json.load(fh))
    raise KeyError(f"unknown caption preset {preset!r} (looked in {_preset_dir()})")


def available_presets() -> list[str]:
    d = _preset_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def _from_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``presets/captions/*.json`` v1 document into the flat shape.

    Keys beginning with ``_`` are documentation and are dropped here, exactly
    once, so no other function has to remember to ignore them.
    """
    p = dict(BASE_PRESET)
    style = {k: v for k, v in (doc.get("ass_style") or {}).items() if not k.startswith("_")}
    font = doc.get("font") or {}
    group = doc.get("grouping") or {}
    over = doc.get("overflow") or {}
    hi = doc.get("highlight") or {}
    pos = doc.get("position") or {}

    p["name"] = doc.get("name", p["name"])
    res = doc.get("play_res") or [p["play_res_x"], p["play_res_y"]]
    p["play_res_x"], p["play_res_y"] = int(res[0]), int(res[1])

    p["font_family"] = font.get("ass_fontname", style.get("Fontname", p["font_family"]))
    p["bold"] = int(font.get("ass_bold", style.get("Bold", 0)))
    p["fontsdir"] = font.get("fontsdir")

    p["font_px"] = float(style.get("Fontsize", p["font_px"]))
    for src, dst in (
        ("PrimaryColour", "primary"),
        ("SecondaryColour", "secondary"),
        ("OutlineColour", "outline_colour"),
        ("BackColour", "back_colour"),
    ):
        if src in style:
            p[dst] = style[src]  # already "&HAABBGGRR"; passed through verbatim
    p["italic"] = int(style.get("Italic", 0))
    p["underline"] = int(style.get("Underline", 0))
    p["strikeout"] = int(style.get("StrikeOut", 0))
    p["scale_x"] = int(style.get("ScaleX", 100))
    p["scale_y"] = int(style.get("ScaleY", 100))
    p["spacing_px"] = float(style.get("Spacing", 0))
    p["angle"] = float(style.get("Angle", 0))
    p["border_style"] = int(style.get("BorderStyle", 1))
    p["outline_px"] = float(style.get("Outline", p["outline_px"]))
    p["shadow_px"] = float(style.get("Shadow", p["shadow_px"]))
    p["alignment"] = int(style.get("Alignment", p["alignment"]))
    p["encoding"] = int(style.get("Encoding", 1))
    if "MarginL" in style:
        p["margin_l_px"] = int(style["MarginL"])
    if "MarginR" in style:
        p["margin_r_px"] = int(style["MarginR"])
    if "MarginV" in style:
        p["margin_v_px"] = int(style["MarginV"])

    if group.get("width_budget_px"):
        p["width_budget_px"] = float(group["width_budget_px"])
    elif group.get("width_budget_pct"):
        p["width_budget_pct"] = _as_fraction(group["width_budget_pct"])
    p["max_lines"] = int(group.get("max_lines", p["max_lines"]))
    p["max_words"] = int(group.get("max_words", p["max_words"]))
    p["min_duration_s"] = float(group.get("min_duration_ms", p["min_duration_s"] * 1000)) / 1000.0
    p["max_duration_s"] = float(group.get("max_duration_ms", p["max_duration_s"] * 1000)) / 1000.0
    p["min_gap_s"] = float(group.get("min_gap_ms", 0)) / 1000.0
    p["split_on_sentence_end"] = bool(group.get("break_on_sentence_punctuation", True))
    p["split_on_clause_end"] = bool(group.get("break_on_clause_punctuation", False))
    p["orphan_min_words"] = int(group.get("avoid_orphan_last_line_words", 0))
    p["_lead_in_ms"] = float(group.get("lead_in_ms", 0))
    p["_hold_out_ms"] = float(group.get("hold_out_ms", 0))

    p["min_font_px"] = float(over.get("min_font_px", p["min_font_px"]))
    p["shrink_step_px"] = float(over.get("shrink_step_px", p["shrink_step_px"]))

    if hi and hi.get("mode") in ("word", "line"):
        p["highlight"] = True
        p["secondary"] = hi.get("active_colour", p["secondary"])
        p["primary"] = hi.get("inactive_colour", p["primary"])
        p["highlight_scale_pct"] = int(hi.get("active_scale_pct", 100))

    if pos.get("mode") == "band":
        centre = pos.get("centre_px")
        if not centre and pos.get("centre_pct"):
            centre = [
                pos["centre_pct"][0] / 100.0 * p["play_res_x"],
                pos["centre_pct"][1] / 100.0 * p["play_res_y"],
            ]
        if centre:
            # Alignment 5 makes libass ignore MarginV, so the band position is
            # only real if it is emitted as an explicit \pos on every event.
            p["pos"] = [int(round(centre[0])), int(round(centre[1]))]

    p["uppercase"] = bool(doc.get("uppercase", False))
    p["strip_terminal_punctuation"] = bool(doc.get("strip_terminal_punctuation", False))
    p["calibrated"] = bool(doc.get("calibrated", False))
    return p


def _as_fraction(v: float) -> float:
    """Accept 0.83 or 83.3 for a percentage. Presets on disk use the latter."""
    f = float(v)
    return f / 100.0 if f > 1.0 else f


# --- grouping -----------------------------------------------------------------


def _budget_px(preset: dict[str, Any]) -> float:
    if preset.get("width_budget_px"):
        return float(preset["width_budget_px"])
    return float(preset["width_budget_pct"]) * float(preset.get("play_res_x", 1920))


def _wrap(tokens: Sequence[str], budget: float, font_px: float, fm: FontMetrics, spacing: float) -> list[str]:
    """Greedy pixel wrap. A token wider than the budget gets a line to itself."""
    lines: list[str] = []
    cur = ""
    for tok in tokens:
        trial = tok if not cur else cur + " " + tok
        if cur and fm.text_width_px(trial, font_px, spacing_px=spacing) > budget:
            lines.append(cur)
            cur = tok
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def _rebalance(lines: list[str], min_last: int, budget: float, font_px: float, fm: FontMetrics, spacing: float) -> list[str]:
    """Pull words down so the last line is not a lonely orphan.

    A one-word second line reads as a mistake; the eye treats it as a new card.
    Only ever moves words DOWN, and only while every line still fits.
    """
    if min_last <= 0 or len(lines) < 2:
        return lines
    out = list(lines)
    guard = 0
    while len(out[-1].split(" ")) < min_last and guard < 8:
        guard += 1
        prev = out[-2].split(" ")
        if len(prev) <= 1:
            break
        moved = prev[-1]
        cand_prev = " ".join(prev[:-1])
        cand_last = moved + " " + out[-1]
        if fm.text_width_px(cand_last, font_px, spacing_px=spacing) > budget:
            break
        out[-2], out[-1] = cand_prev, cand_last
    return out


def _word_text(w: dict[str, Any]) -> str:
    return str(w.get("word", w.get("text", ""))).strip()


def _usable(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop words with no usable timing rather than emitting a zero-length cue."""
    out: list[dict[str, Any]] = []
    for w in words:
        if _word_text(w) == "":
            continue
        s, e = w.get("start"), w.get("end")
        if s is None or e is None:
            continue
        if float(e) <= float(s):
            w = {**w, "end": float(s) + 0.04}
        out.append(w)
    return out


def _display_token(tok: str, preset: dict[str, Any]) -> str:
    if preset.get("uppercase"):
        # German .upper() maps sz to SS, which is the correct uppercase form.
        tok = tok.upper()
    return tok


def group_words(words: list[dict], preset: dict, tb: Timebase) -> list[dict]:
    """Words -> caption cues, split on a PIXEL-width budget.

    ``preset["width_budget_px"]`` wins if present; otherwise the budget is
    ``width_budget_pct * play_res_x``. build_ass() injects the project's real
    PlayResX before calling, which is why this function needs no project.

    A cue whose widest line still exceeds the budget after wrapping (one long
    compound noun) auto-shrinks: the returned ``font_px`` steps down by
    ``shrink_step_px`` until it fits or hits ``min_font_px``, ``shrunk`` is
    True, and ``overflow`` is True if even the floor was not enough.
    """
    p = load_preset(preset)
    ws = _usable(words)
    if not ws:
        return []
    budget = _budget_px(p)
    fm = _as_metrics((p["font_family"], p["bold"]))
    spacing = float(p.get("spacing_px", 0.0))
    max_lines = int(p["max_lines"])
    base_px = float(p["font_px"])
    min_px = float(p["min_font_px"])
    step = max(1.0, float(p.get("shrink_step_px", 2)))
    orphan = int(p.get("orphan_min_words", 0))
    lead_s = float(p.get("_lead_in_ms", 0.0)) / 1000.0 + tb.frames_to_seconds(int(p.get("lead_frames", 0)))
    tail_s = float(p.get("_hold_out_ms", 0.0)) / 1000.0 + tb.frames_to_seconds(int(p.get("tail_frames", 0)))

    cues: list[dict] = []
    cur: list[tuple[int, dict]] = []

    def widest(lines: list[str], px: float) -> float:
        return max((fm.text_width_px(ln, px, spacing_px=spacing) for ln in lines), default=0.0)

    def flush() -> None:
        if not cur:
            return
        tokens = [_display_token(_word_text(w), p) for _, w in cur]
        if p.get("strip_terminal_punctuation") and tokens[-1].endswith("."):
            # A trailing full stop on a two-word card reads as a pause the audio
            # does not contain. ? and ! change the reading and are kept.
            tokens[-1] = tokens[-1].rstrip(".")
            if not tokens[-1]:
                tokens.pop()
        if not tokens:
            return
        font_px = base_px
        lines = _wrap(tokens, budget, font_px, fm, spacing)
        shrunk = False
        while widest(lines, font_px) > budget and font_px - step >= min_px:
            font_px -= step
            lines = _wrap(tokens, budget, font_px, fm, spacing)
            shrunk = True
        overflow = widest(lines, font_px) > budget
        lines = _rebalance(lines, orphan, budget, font_px, fm, spacing)

        t_in = float(cur[0][1]["start"]) - lead_s
        t_out = float(cur[-1][1]["end"]) + tail_s
        in_f, out_f = tb.snap_pair(max(0.0, t_in), t_out)
        min_frames = max(1, tb.seconds_to_frames_delta(float(p["min_duration_s"])))
        if out_f - in_f < min_frames:
            out_f = in_f + min_frames
        cues.append(
            {
                "index": len(cues),
                "word_range": [cur[0][0], cur[-1][0] + 1],
                "words": [
                    {
                        "word": _display_token(_word_text(w), p),
                        "start": float(w["start"]),
                        "end": float(w["end"]),
                        "index": i,
                    }
                    for i, w in cur
                ],
                "text": " ".join(tokens),
                "lines": lines,
                "start": float(cur[0][1]["start"]),
                "end": float(cur[-1][1]["end"]),
                "start_frame": in_f,
                "end_frame": out_f,
                "font_px": int(font_px),
                "shrunk": shrunk,
                "overflow": overflow,
                "width_px": round(widest(lines, font_px), 2),
                "budget_px": round(budget, 2),
            }
        )

    clause_end = ",;:"
    for i, w in enumerate(ws):
        if not cur:
            cur = [(i, w)]
            continue
        prev = cur[-1][1]
        gap = float(w["start"]) - float(prev["end"])
        span = float(w["end"]) - float(cur[0][1]["start"])
        prev_text = _word_text(prev)
        if gap >= float(p["split_gap_s"]):
            force = True
        elif p["split_on_sentence_end"] and prev_text[-1:] in ".?!":
            force = True
        elif p["split_on_clause_end"] and prev_text[-1:] in clause_end and len(cur) >= 3:
            force = True
        elif span > float(p["max_duration_s"]):
            force = True
        elif len(cur) >= int(p["max_words"]):
            force = True
        else:
            trial = [_display_token(_word_text(x[1]), p) for x in cur] + [_display_token(_word_text(w), p)]
            force = len(_wrap(trial, budget, base_px, fm, spacing)) > max_lines
        if force:
            flush()
            cur = [(i, w)]
        else:
            cur.append((i, w))
    flush()

    # min_duration_s and hold_out can push a cue over its successor. The later
    # cue always wins: its start is a real spoken onset, the extension was not.
    gap_frames = max(0, tb.seconds_to_frames_delta(float(p.get("min_gap_s", 0.0))))
    for a, b in zip(cues, cues[1:]):
        limit = b["start_frame"] - gap_frames
        if a["end_frame"] > limit:
            a["end_frame"] = max(a["start_frame"] + 1, limit)
    return cues


# --- ASS emission -------------------------------------------------------------


def ass_colour(rgb: str, alpha: float = 0.0) -> str:
    """``#RRGGBB`` + 0..1 transparency -> ASS ``&HAABBGGRR``.

    A value that is already an ASS literal passes through untouched, so preset
    files can carry either spelling.

    ASS stores TRANSPARENCY, not opacity: AA=00 is fully visible. Getting that
    backwards produces captions that are present but invisible, which reads as
    a font failure and sends you hunting in the wrong place.
    """
    if rgb.startswith("&H") or rgb.startswith("&h"):
        return rgb
    s = rgb.lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"expected #RRGGBB or &HAABBGGRR, got {rgb!r}")
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    a = max(0, min(255, int(round(float(alpha) * 255))))
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _bgr(colour: str) -> str:
    r"""Any colour spelling -> ``BBGGRR`` for an inline ``\1c&H..&`` override.

    Inline colour overrides carry NO alpha byte. Pasting the 8-digit style form
    in there makes libass read the alpha as blue and the caption turns a colour
    nobody chose.
    """
    return ass_colour(colour, 0.0)[-6:]


def ass_time(frames: int, tb: Timebase) -> str:
    """Frames -> ``H:MM:SS.CC``. ASS resolution is centiseconds, not frames.

    Rounded with exact integer arithmetic so 30000/1001 does not drift a
    centisecond every few minutes.
    """
    total_cs = (frames * 100 * tb.fps_den * 2 + tb.fps_num) // (tb.fps_num * 2)
    cs = total_cs % 100
    total_s = total_cs // 100
    return f"{total_s // 3600}:{(total_s // 60) % 60:02d}:{total_s % 60:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    r"""Make arbitrary transcript text safe as an ASS event body.

    ``{`` opens an override block, so an unescaped brace out of a transcript
    swallows everything up to the next ``}``. Hard newlines become ``\N``
    because the events section is line-based.
    """
    out = text.replace("{", r"\{").replace("}", r"\}")
    return out.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def _project_data(project: Any) -> dict[str, Any]:
    """The plain dict behind a Project, or the dict itself.

    Project stores it as ``.data``. Reaching for the wrong attribute name fails
    silently -- the fallback is the Project object, which still answers .get(),
    so a resolution lookup returns the 1920x1080 default and every px value in
    the preset ends up scaled against the wrong frame.
    """
    data = getattr(project, "data", None)
    return data if isinstance(data, dict) else project


def _project_view(project: Any) -> tuple[Timebase, int, int, str]:
    """Accept a Project or a plain dict, so this module is testable standalone."""
    data = _project_data(project)
    if hasattr(project, "timebase"):
        tb = project.timebase()
    else:
        tb = Timebase.from_dict(data.get("timebase", {"fps_num": 30, "fps_den": 1}))
    out = data.get("output") or {}
    res = out.get("resolution") or [1920, 1080]
    return tb, int(res[0]), int(res[1]), str(out.get("platform", "youtube"))


def timeline_words(words: list[dict], project: Any) -> list[dict]:
    """Re-time source-clock words onto the CUT timeline. Returns a new list.

    The transcript is on the SOURCE clock; the rendered master is on the
    timeline clock. Every removal between the two is a growing offset, so an
    ASS built straight from the transcript drifts by the total removed duration
    AND still captions words that were cut out. Measured on the integration
    fixture: the last transcript word sat at 28.33 s against a 22.77 s master,
    with "ähm", "sozusagen" and an abandoned retake still in the caption stream.

    Words falling outside every enabled segment are DROPPED -- they are not in
    the picture. A word whose end runs past its segment's out point is clamped
    to that segment's last frame. A project with no enabled segments (a
    transcript that has not been cut yet) passes through unchanged, so
    ``ve captions`` before ``ve cut`` still previews something.
    """
    data = _project_data(project)
    segments = [s for s in (data.get("segments") or []) if s.get("enabled", True)]
    if not segments:
        return list(words)

    tb, _w, _h, _p = _project_view(project)
    fps = tb.fps
    transcript_source = str(((data.get("transcript") or {}).get("source_id")) or "")

    # (src_lo_s, src_hi_s, tl_in_frames, src_in_frames, speed)
    spans: list[tuple[float, float, int, int, float]] = []
    tl = 0
    for seg in sorted(segments, key=lambda s: (int(s.get("order", 0)), str(s.get("id")))):
        src_in = int(seg["src_in_frames"])
        src_out = int(seg["src_out_frames"])
        speed = float(seg.get("speed", 1.0) or 1.0)
        length = (src_out - src_in) if speed == 1.0 else int(round((src_out - src_in) / speed))
        # Only a segment cut from the transcript's own source carries its words.
        # A B-roll insert from source B advances the clock but captions nothing.
        if not transcript_source or str(seg.get("source_id")) == transcript_source:
            spans.append((src_in / fps, src_out / fps, tl, src_in, speed))
        tl += length

    # A word is assigned to the span it OVERLAPS most, not the span its start
    # falls in. The cut points carry pad_out, so a removed filler's first
    # milliseconds sit inside the preceding kept segment: matching on start
    # alone kept "ähm" and "sozusagen" in the caption stream even though the
    # picture no longer contains them. Measured on the integration fixture --
    # "ähm" spans source frames 141.3-159.9 against a cut at frame 142.
    keep_ratio = 0.5

    out: list[dict] = []
    for word in words:
        start = word.get("start")
        if start is None:
            continue
        w_lo = float(start)
        w_hi = float(word.get("end") if word.get("end") is not None else w_lo)
        span_len = max(w_hi - w_lo, 1.0 / fps)

        best: tuple[float, float, float, int, int, float] | None = None
        for lo, hi, tl_in, src_in, speed in spans:
            overlap = min(w_hi, hi) - max(w_lo, lo)
            if overlap <= 0.0:
                continue
            if best is None or overlap > best[0]:
                best = (overlap, lo, hi, tl_in, src_in, speed)
        if best is None or best[0] / span_len < keep_ratio:
            continue

        _ov, lo, hi, tl_in, src_in, speed = best
        clipped_lo = max(w_lo, lo)
        clipped_hi = min(max(w_hi, clipped_lo), hi)
        mapped_start = (tl_in + (clipped_lo * fps - src_in) / speed) / fps
        mapped_end = (tl_in + (clipped_hi * fps - src_in) / speed) / fps
        out.append({
            **word,
            "start": round(mapped_start, 4),
            "end": round(max(mapped_end, mapped_start), 4),
        })
    return out


def _apply_overrides(words: list[dict], project: Any) -> list[dict]:
    """Apply ``captions.overrides`` -- the escape hatch for suppressed numerals.

    --suppress_numerals is mandatory upstream (it removes the unalignable class
    that makes word.start a KeyError), and its cost is that the transcript says
    "zwanzig" where the caption should read "20". This is where that is fixed.
    """
    data = _project_data(project)
    caps = data.get("captions") or {}
    overrides = caps.get("overrides") or []
    if not overrides:
        return list(words)
    by_index = {int(o["word_index"]): str(o["text"]) for o in overrides if "word_index" in o}
    return [({**w, "word": by_index[i]} if i in by_index else w) for i, w in enumerate(words)]


def build_ass(words: list[dict], project: Any, preset: str | dict | None = None,
              *, timeline: bool = True) -> str:
    """Word timings -> a complete ASS document with canonical Format lines.

    Returns the text; write it with write_ass() so encoding and line endings are
    right. PlayResX/PlayResY are forced to the project's output resolution: if
    they differ, libass scales every px value and the pixel budget silently
    stops meaning pixels.
    """
    p = load_preset(preset)
    tb, width, height, _platform = _project_view(project)
    if (p["play_res_x"], p["play_res_y"]) != (width, height):
        # The preset was authored for another frame size. Scale every px value
        # rather than letting libass do it behind the budget's back.
        p = _rescale(p, width, height)

    words = _apply_overrides(list(words), project)
    if timeline:
        # The master is on the timeline clock; the transcript is not.
        words = timeline_words(words, project)
    cues = group_words(words, p, tb)

    ml = int(p["margin_l_px"]) if p.get("margin_l_px") is not None else int(round(p["margin_l_pct"] * width))
    mr = int(p["margin_r_px"]) if p.get("margin_r_px") is not None else int(round(p["margin_r_pct"] * width))
    mv = int(p["margin_v_px"]) if p.get("margin_v_px") is not None else int(round(p["margin_v_pct"] * height))

    style_values = [
        STYLE_NAME,
        str(p["font_family"]),
        _num(p["font_px"]),
        ass_colour(p["primary"], p["primary_alpha"]),
        ass_colour(p["secondary"], p["primary_alpha"]),
        ass_colour(p["outline_colour"], p["outline_alpha"]),
        ass_colour(p["back_colour"], p["back_alpha"]),
        str(int(p["bold"])),
        str(int(p["italic"])),
        str(int(p["underline"])),
        str(int(p["strikeout"])),
        str(int(p["scale_x"])),
        str(int(p["scale_y"])),
        _num(p["spacing_px"]),
        _num(p["angle"]),
        str(int(p["border_style"])),
        _num(p["outline_px"]),
        _num(p["shadow_px"]),
        str(int(p["alignment"])),
        str(ml),
        str(mr),
        str(mv),
        str(int(p["encoding"])),
    ]
    if len(style_values) != len(STYLE_FORMAT):  # pragma: no cover - structural
        raise AssertionError("style row must match STYLE_FORMAT field for field")

    lines: list[str] = [
        "[Script Info]",
        "; Generated by vedit.captions -- regenerate, do not hand-edit.",
        f"; preset={p.get('name')} calibrated={str(bool(p.get('calibrated', False))).lower()}",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        # WrapStyle 2 = never auto-wrap. The lines were already wrapped on
        # measured pixel widths; letting libass re-wrap would undo the budget.
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: " + ", ".join(STYLE_FORMAT),
        "Style: " + ",".join(style_values),
        "",
        "[Events]",
        "Format: " + ", ".join(EVENT_FORMAT),
    ]
    for cue in cues:
        lines.extend(_events_for_cue(cue, p, tb))
    return "\r\n".join(lines) + "\r\n"


def _rescale(p: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    q = dict(p)
    sx = width / float(p["play_res_x"])
    sy = height / float(p["play_res_y"])
    s = min(sx, sy)
    for k in ("font_px", "min_font_px", "outline_px", "shadow_px", "spacing_px"):
        q[k] = float(p[k]) * s
    if p.get("width_budget_px"):
        q["width_budget_px"] = float(p["width_budget_px"]) * sx
    for k, f in (("margin_l_px", sx), ("margin_r_px", sx), ("margin_v_px", sy)):
        if p.get(k) is not None:
            q[k] = int(round(float(p[k]) * f))
    if p.get("pos"):
        q["pos"] = [int(round(p["pos"][0] * sx)), int(round(p["pos"][1] * sy))]
    q["play_res_x"], q["play_res_y"] = width, height
    return q


def _num(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _events_for_cue(cue: dict, p: dict, tb: Timebase) -> list[str]:
    prefix = ""
    if p.get("pos"):
        prefix += "\\pos(%d,%d)" % (int(p["pos"][0]), int(p["pos"][1]))
    if int(cue["font_px"]) != int(p["font_px"]):
        prefix += "\\fs%d" % int(cue["font_px"])  # per-cue shrink for a compound noun
    prefix = "{%s}" % prefix if prefix else ""

    if not p.get("highlight"):
        return [_dialogue(cue["start_frame"], cue["end_frame"], prefix + "\\N".join(ass_escape(ln) for ln in cue["lines"]), tb)]

    # Highlight mode: one event per word, the whole card redrawn each time with
    # the active word recoloured. Deliberately not \k -- \k times relative to
    # the event start, quantises to centiseconds, and stops being correct the
    # moment an upstream cut retimes the cue.
    active = _bgr(p["secondary"])
    inactive = _bgr(p["primary"])
    scale = int(p.get("highlight_scale_pct", 100))
    events: list[str] = []
    words = cue["words"]
    for i, w in enumerate(words):
        start_f = cue["start_frame"] if i == 0 else tb.snap_in(float(w["start"]))
        end_f = cue["end_frame"] if i == len(words) - 1 else tb.snap_in(float(words[i + 1]["start"]))
        start_f = max(cue["start_frame"], min(start_f, cue["end_frame"] - 1))
        end_f = min(cue["end_frame"], max(end_f, start_f + 1))
        parts: list[str] = []
        seen = 0
        for line in cue["lines"]:
            rendered = []
            for tok in line.split(" "):
                if seen == i:
                    on = "\\1c&H%s&" % active
                    off = "\\1c&H%s&" % inactive
                    if scale != 100:
                        on += "\\fscx%d\\fscy%d" % (scale, scale)
                        off += "\\fscx100\\fscy100"
                    rendered.append("{%s}%s{%s}" % (on, ass_escape(tok), off))
                else:
                    rendered.append(ass_escape(tok))
                seen += 1
            parts.append(" ".join(rendered))
        events.append(_dialogue(start_f, end_f, prefix + "\\N".join(parts), tb))
    return events


def _dialogue(start_f: int, end_f: int, text: str, tb: Timebase) -> str:
    # Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
    return f"Dialogue: 0,{ass_time(start_f, tb)},{ass_time(end_f, tb)},{STYLE_NAME},,0,0,0,,{text}"


def write_ass(text: str, path: str) -> str:
    """Write an ASS document as UTF-8 without BOM. Returns the path.

    No BOM: libass tolerates one, but it lands inside the first section header
    for anything else that reads the file. Umlauts survive because the encoding
    is explicit at both ends -- verified by burning and reading the pixels back.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return path


def validate_ass(text: str) -> list[str]:
    """Structural check of a generated ASS. An empty list means well-formed.

    Exists because the failure is silent: libass exits 0 on a malformed Format
    line and merely renders the wrong thing -- a stray leading comma in the
    text, no outline, no bold.
    """
    problems: list[str] = []
    section = ""
    saw_style_format = saw_event_format = False
    style_n = event_n = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            continue
        if not line or line.startswith(";"):
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        if section.startswith("[v4+ styles"):
            if key == "Format":
                saw_style_format = True
                fields = tuple(f.strip() for f in rest.split(","))
                style_n = len(fields)
                if fields != STYLE_FORMAT:
                    problems.append(
                        f"line {lineno}: V4+ Format has {style_n} fields and does not match the "
                        f"canonical {len(STYLE_FORMAT)}-field list"
                    )
            elif key == "Style":
                n = len(rest.split(","))
                if n != style_n:
                    problems.append(f"line {lineno}: Style has {n} values, Format declares {style_n}")
        elif section.startswith("[events"):
            if key == "Format":
                saw_event_format = True
                fields = tuple(f.strip() for f in rest.split(","))
                event_n = len(fields)
                if fields != EVENT_FORMAT:
                    problems.append(
                        f"line {lineno}: Events Format has {event_n} fields and does not match the "
                        f"canonical {len(EVENT_FORMAT)}-field list"
                    )
            elif key in ("Dialogue", "Comment"):
                n = len(rest.split(",", event_n - 1))
                if n != event_n:
                    problems.append(f"line {lineno}: {key} has {n} fields, Format declares {event_n}")
    if not saw_style_format:
        problems.append("no [V4+ Styles] Format line")
    if not saw_event_format:
        problems.append("no [Events] Format line")
    return problems


# --- burn-in ------------------------------------------------------------------


def subtitles_filter(ass_path: str, fonts: str | None = None) -> str:
    r"""The ``subtitles=`` filter string, escaped for Windows paths.

    Both values use the quoted single-escape form from vedit.ffmpeg -- the only
    spelling measured to survive spaces, commas and umlauts in both the outer
    filtergraph parse and the inner option parse::

        subtitles='C\:/.../captions.ass':fontsdir='C\:/.../assets/fonts'

    fontsdir is always passed. Without it libass resolves via DirectWrite, which
    means a bundled font that is not installed system-wide renders as Arial with
    exit 0 and no log line.
    """
    d = fonts or fonts_dir()
    return (
        f"subtitles={escape_filter_path(os.path.abspath(ass_path))}"
        f":fontsdir={escape_filter_path(os.path.abspath(d))}"
    )


def burn_in_args(
    video_path: str,
    ass_path: str,
    out_path: str,
    *,
    fonts: str | None = None,
    v_codec: str = "libx264",
    extra_video: Sequence[str] | None = None,
    audio: Sequence[str] = ("-c:a", "copy"),
) -> list[str]:
    """The exact argv for a burn-in. Separate from burn_in() so QC can log it."""
    args = [
        FFMPEG,
        "-y",
        "-i",
        os.path.abspath(video_path),
        "-vf",
        subtitles_filter(ass_path, fonts),
        "-c:v",
        v_codec,
    ]
    args += list(extra_video or ("-preset", "veryfast", "-crf", "18"))
    # yuv420p unconditionally: libass composites in RGB, and 8-bit 4:2:0 is the
    # only pixel format every player and platform accepts.
    args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    args += list(audio)
    args += [os.path.abspath(out_path)]
    return args


def burn_in(
    video_path: str,
    ass_path: str,
    out_path: str,
    *,
    fonts: str | None = None,
    v_codec: str = "libx264",
    extra_video: Sequence[str] | None = None,
    audio: Sequence[str] = ("-c:a", "copy"),
) -> str:
    """Burn an ASS file into a video. Returns the output path.

    Validates the ASS first. libass renders a malformed file happily and exits
    0, so before the encode is the only place the error can still be caught.
    """
    with open(ass_path, "r", encoding="utf-8") as fh:
        problems = validate_ass(fh.read())
    if problems:
        raise ValueError("malformed ASS, refusing to burn:\n  " + "\n  ".join(problems))
    args = burn_in_args(
        video_path, ass_path, out_path, fonts=fonts, v_codec=v_codec, extra_video=extra_video, audio=audio
    )
    run(args)
    return os.path.abspath(out_path)


__all__ = [
    "STYLE_FORMAT",
    "EVENT_FORMAT",
    "STYLE_NAME",
    "FONT_TABLE",
    "BASE_PRESET",
    "FontMetrics",
    "FontMetricsError",
    "fonts_dir",
    "font_path",
    "font_family_name",
    "load_font",
    "check_font_table",
    "estimate_text_width_px",
    "load_preset",
    "available_presets",
    "group_words",
    "build_ass",
    "timeline_words",
    "write_ass",
    "validate_ass",
    "ass_colour",
    "ass_time",
    "ass_escape",
    "subtitles_filter",
    "burn_in_args",
    "burn_in",
]
