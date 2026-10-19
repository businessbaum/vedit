"""WhisperX output -> the one word-record shape the rest of the pipeline reads.

WhisperX's raw JSON is not allowed to be the source of truth. Two reasons, both
measured rather than assumed:

* A word can legitimately have **no** ``start``/``end``/``score``. ``alignment.py``
  only sets those keys ``if not np.isnan(...)``, so any word whose characters
  are outside the wav2vec2 dictionary -- digits, ``%``, ``EUR`` -- comes back
  bare. Anything keying on ``word["start"]`` KeyErrors on a real transcript.
* The per-segment ``words`` lists hide restarts that straddle a Whisper segment
  boundary. Everything downstream works on ONE flattened list, so a retake that
  spans a segment join is still visible.

So :func:`normalize_whisperx` runs once and produces records with all seven
keys always present, plus ``interpolated``: the single boolean that lets QC
flag a cut placed on a guessed timestamp, which is exactly where a
word-boundary rough cut goes wrong.

Word indices into the returned list are the pipeline's stable coordinate
system: ``story.blocks[].word_range``, ``segments[].word_range`` and
``captions.overrides[].word_index`` all point in here. **Every ``word_range``
is half-open ``[first, last + 1)``**, the same convention as the frame ranges,
because half-open is what makes adjacent ranges concatenate without an
off-by-one.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Sequence

from . import fillers as _fillers

# A word whose alignment score is below this had its letters poorly matched to
# the audio, so its BOUNDARIES are unreliable (CONTRACT section 8). It says
# nothing about the word being a filler -- low score correlates with rare words
# and proper nouns, the opposite.
LOW_SCORE = 0.35

WORD_KEYS = ("word", "start", "end", "score", "seg_idx", "speaker", "interpolated")


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

def _text_of(raw: dict) -> str:
    # whisperx writes "word"; the ffmpeg whisper-filter fallback writes "text".
    value = raw.get("word")
    if value is None:
        value = raw.get("text")
    return "" if value is None else str(value).strip()


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN survives float() and then poisons every comparison downstream.
    return None if out != out else out


def _record(raw: dict, seg_idx: int, seg_speaker: Any) -> dict:
    start = _float_or_none(raw.get("start"))
    end = _float_or_none(raw.get("end"))
    score = _float_or_none(raw.get("score"))
    speaker = raw.get("speaker", seg_speaker)
    return {
        "word": _text_of(raw),
        "start": start,
        "end": end,
        # Missing score means the word was never force-aligned. 0.0 makes it fall
        # under LOW_SCORE, which widens padding around it -- the behaviour we
        # want for a word whose boundaries were guessed.
        "score": 0.0 if score is None else score,
        "seg_idx": seg_idx,
        "speaker": None if speaker is None else str(speaker),
        "interpolated": start is None or end is None,
    }


def _interpolate(records: list[dict], lo: float | None, hi: float | None) -> None:
    """Fill missing start/end in place, proportionally to character count.

    Anchors are the words that carry BOTH timestamps. Words before the first
    anchor and after the last one fall back to the enclosing segment bounds; a
    whole segment with no anchor at all and no bounds degenerates to a
    zero-length span at ``lo``, which QC will pick up as a suspicious record
    rather than crash on.
    """
    n = len(records)
    if n == 0:
        return
    anchors = [i for i, r in enumerate(records) if r["start"] is not None and r["end"] is not None]

    if not anchors:
        span_lo = lo if lo is not None else 0.0
        span_hi = hi if hi is not None else span_lo
        _spread(records, 0, n, span_lo, span_hi)
        return

    first, last = anchors[0], anchors[-1]
    if first > 0:
        head_lo = lo if lo is not None else records[first]["start"]
        _spread(records, 0, first, head_lo, records[first]["start"])
    if last < n - 1:
        tail_hi = hi if hi is not None else records[last]["end"]
        _spread(records, last + 1, n, records[last]["end"], tail_hi)
    for a, b in zip(anchors, anchors[1:]):
        if b > a + 1:
            _spread(records, a + 1, b, records[a]["end"], records[b]["start"])


def _spread(records: list[dict], lo_i: int, hi_i: int, t0: float, t1: float) -> None:
    weights = [max(1, len(r["word"])) for r in records[lo_i:hi_i]]
    total = sum(weights)
    span = max(0.0, (t1 or 0.0) - (t0 or 0.0))
    cursor = t0 or 0.0
    for r, w in zip(records[lo_i:hi_i], weights):
        width = span * w / total if total else 0.0
        r["start"] = cursor
        r["end"] = cursor + width
        r["interpolated"] = True
        cursor += width


def normalize_whisperx(raw: dict) -> list[dict]:
    """WhisperX result dict -> flat word records.

    Accepts the ``--output_format json`` document (``segments`` +
    ``word_segments`` + ``language``) and also a bare ``{"words": [...]}`` or a
    plain list, so the degraded ffmpeg-whisper tier can be fed through the same
    door.
    """
    if isinstance(raw, list):
        raw = {"word_segments": raw}
    if not isinstance(raw, dict):
        raise TypeError(f"normalize_whisperx expects a dict, got {type(raw).__name__}")

    out: list[dict] = []
    segments = raw.get("segments")
    if isinstance(segments, list) and any(s.get("words") for s in segments if isinstance(s, dict)):
        for seg_idx, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            words = seg.get("words") or []
            recs = [_record(w, seg_idx, seg.get("speaker")) for w in words if isinstance(w, dict)]
            recs = [r for r in recs if r["word"]]
            if not recs:
                continue
            _interpolate(recs, _float_or_none(seg.get("start")), _float_or_none(seg.get("end")))
            out.extend(recs)
    else:
        flat = raw.get("word_segments") or raw.get("words") or []
        recs = [_record(w, 0, w.get("speaker")) for w in flat if isinstance(w, dict)]
        recs = [r for r in recs if r["word"]]
        _interpolate(recs, None, None)
        out.extend(recs)

    _repair_monotonicity(out)
    return out


def _repair_monotonicity(records: list[dict]) -> None:
    """Fix only what is structurally impossible; leave real overlap alone.

    A word whose ``end`` precedes its ``start`` is a bug and gets a zero-length
    span. Words that overlap their neighbour are NOT repaired: forced alignment
    across a segment boundary genuinely produces overlap, and silently shoving
    timestamps around would move cut points. cut.py clamps negative gaps to
    zero, which is the honest handling.
    """
    for r in records:
        if r["start"] is None:
            r["start"] = 0.0
            r["interpolated"] = True
        if r["end"] is None or r["end"] < r["start"]:
            r["end"] = r["start"]
            r["interpolated"] = True


def load_words(path: str) -> list[dict]:
    """Read a words sidecar. Accepts raw WhisperX or an already-normalized list.

    UTF-8 is forced: whisperx writes ``ensure_ascii=False`` and the Windows
    default codepage would mangle every umlaut on read.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "interpolated" in data[0]:
        return [dict(w) for w in data]
    return normalize_whisperx(data)


def save_words(words: Sequence[dict], path: str) -> str:
    """Write normalized records next to the cache. Atomic, UTF-8, no BOM."""
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(list(words), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# reading views
# --------------------------------------------------------------------------- #

_NO_SPACE_BEFORE = ".,!?;:…)»”’%"
_NO_SPACE_AFTER = "(«„‘"


def flatten_text(words: Iterable[dict]) -> str:
    """Word records -> readable prose.

    Denormalized for reading only, never authoritative (CONTRACT section 3).
    Punctuation that whisperx emitted as its own token is re-attached so the
    story pass does not read ``Test .`` and think a word is missing.
    """
    parts: list[str] = []
    for w in words:
        token = str(w.get("word", "")).strip()
        if not token:
            continue
        if parts and (token[0] in _NO_SPACE_BEFORE or parts[-1][-1] in _NO_SPACE_AFTER):
            parts[-1] = parts[-1] + token
        else:
            parts.append(token)
    return " ".join(parts)


def word_span(words: Sequence[dict], word_range: Sequence[int]) -> tuple[float, float]:
    """Half-open ``[a, b)`` word range -> ``(start_s, end_s)``."""
    a, b = int(word_range[0]), int(word_range[1])
    a = max(0, min(a, len(words)))
    b = max(a, min(b, len(words)))
    if b <= a:
        return (0.0, 0.0)
    return (float(words[a]["start"]), float(words[b - 1]["end"]))


def gap_before(words: Sequence[dict], i: int) -> float:
    """Silence between word ``i-1`` and word ``i``. Never negative."""
    if i <= 0 or i >= len(words):
        return 0.0
    return max(0.0, float(words[i]["start"]) - float(words[i - 1]["end"]))


def duration(words: Sequence[dict], i: int) -> float:
    return max(0.0, float(words[i]["end"]) - float(words[i]["start"]))


def format_timestamp(seconds: float) -> str:
    """``MM:SS.mmm``, or ``H:MM:SS.mmm`` past the hour. Invariant culture."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(seconds * 1000 + 0.5)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    if h:
        return f"{h}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{m:02d}:{s:02d}.{ms:03d}"


# --------------------------------------------------------------------------- #
# the LLM-facing rendering
# --------------------------------------------------------------------------- #

def words_to_markdown(
    words: Sequence[dict],
    *,
    title: str | None = None,
    language: str = "de",
    pause_threshold: float = 0.55,
    max_line_words: int = 24,
    blocks: Sequence[dict] | None = None,
    marks: dict[int, str] | None = None,
    show_gaps: bool = True,
    show_confidence: bool = True,
    header: bool = True,
) -> str:
    """Render a numbered, timestamped transcript for a model to reason over.

    This is what the story pass and the cut skills actually read, so the format
    is chosen for one job: let a model quote back an exact half-open
    ``word_range`` without counting words by hand.

    Every line therefore states the index of its first and last word and its
    time span. Lines break at sentence ends, at pauses of at least
    ``pause_threshold`` seconds, and at ``max_line_words`` -- so a line is
    roughly a breath group, which is also the unit a human reviewer thinks in.

    ``blocks`` inserts story headings (``story.blocks`` from project.json).
    ``marks`` annotates individual words -- pass ``{i: "A:remove"}`` built from
    :func:`vedit.cut.detect_fillers` to render the before/after view.
    """
    marks = marks or {}
    lines: list[str] = []

    if header:
        total = len(words)
        if total:
            t0 = float(words[0]["start"])
            t1 = float(words[-1]["end"])
        else:
            t0 = t1 = 0.0
        lines.append(f"# Transcript{': ' + title if title else ''}")
        lines.append("")
        lines.append(
            f"- language: `{language}` | words: {total} | "
            f"span: {format_timestamp(t0)} - {format_timestamp(t1)} ({t1 - t0:.2f} s)"
        )
        lines.append("- line format: `[first-last | start -> end] text` (word indices are 0-based)")
        lines.append(
            "- a `word_range` is HALF-OPEN: the line `[0012-0019 ...]` is `word_range: [12, 20]`"
        )
        if show_confidence:
            lines.append(
                f"- `~` after a word: alignment score < {LOW_SCORE} or timestamp interpolated, "
                "so its boundaries are unreliable and no cut may be placed inside it"
            )
        if show_gaps:
            lines.append(f"- `-- pause N.NN s --`: silence of at least {pause_threshold:.2f} s")
        if marks:
            lines.append("- `{...}` after a word: detector annotation")
        lines.append("")

    block_at: dict[int, dict] = {}
    for blk in blocks or []:
        rng = blk.get("word_range") or [0, 0]
        block_at[int(rng[0])] = blk

    open_block: dict | None = None
    line_words: list[int] = []

    def flush() -> None:
        if not line_words:
            return
        first, last = line_words[0], line_words[-1]
        text = _render_line(words, line_words, marks, show_confidence)
        stamp = f"{format_timestamp(float(words[first]['start']))} -> {format_timestamp(float(words[last]['end']))}"
        lines.append(f"[{first:04d}-{last:04d} | {stamp}] {text}")
        line_words.clear()

    for i, w in enumerate(words):
        blk = block_at.get(i)
        if blk is not None:
            flush()
            if open_block is not None:
                lines.append("")
            open_block = blk
            rng = blk.get("word_range") or [i, i]
            decision = blk.get("decision", "keep")
            label = blk.get("label") or blk.get("id") or "block"
            lines.append(
                f"## {blk.get('id', 'blk')} - {label}  "
                f"[{int(rng[0])}, {int(rng[1])}) decision={decision}"
                + (f" order={blk['order']}" if "order" in blk else "")
            )
            if blk.get("rationale"):
                lines.append(f"> {blk['rationale']}")
            lines.append("")

        gap = gap_before(words, i)
        if i > 0 and gap >= pause_threshold:
            flush()
            if show_gaps:
                lines.append(f"    -- pause {gap:.2f} s --")

        line_words.append(i)

        if len(line_words) >= max_line_words or _fillers.is_sentence_end(str(w.get("word", ""))):
            flush()

    flush()
    return "\n".join(lines).rstrip() + "\n"


def _render_line(words: Sequence[dict], idx: Sequence[int],
                 marks: dict[int, str], show_confidence: bool) -> str:
    parts: list[str] = []
    for i in idx:
        w = words[i]
        token = str(w.get("word", "")).strip()
        if not token:
            continue
        suffix = ""
        if show_confidence and (w.get("interpolated") or float(w.get("score", 1.0)) < LOW_SCORE):
            suffix += "~"
        if i in marks:
            suffix += "{" + marks[i] + "}"
        if parts and token[0] in _NO_SPACE_BEFORE and not suffix:
            parts[-1] = parts[-1] + token
        else:
            parts.append(token + suffix)
    return " ".join(parts)


def segments_to_markdown(segments: Sequence[dict], words: Sequence[dict]) -> str:
    """Render a built segment list as a reviewable before/after table.

    Disabled rows are what the machine proposes to drop; flipping one boolean
    in project.json puts them back, which is the whole point of never deleting.
    """
    lines = [
        "| order | id | frames | src | words | on | stage/rule | text |",
        "| ---: | --- | ---: | --- | --- | :-: | --- | --- |",
    ]
    for seg in segments:
        prov = seg.get("provenance") or {}
        rng = seg.get("word_range") or [0, 0]
        frames = int(seg["src_out_frames"]) - int(seg["src_in_frames"])
        text = (seg.get("text") or "").replace("|", "/")
        if len(text) > 70:
            text = text[:67] + "..."
        rule = f"{prov.get('stage', '?')}/{prov.get('rule', '?')}"
        lines.append(
            f"| {seg.get('order', '')} | {seg.get('id', '')} | {frames} | "
            f"{seg['src_in_frames']}-{seg['src_out_frames']} | "
            f"[{int(rng[0])},{int(rng[1])}) | {'x' if seg.get('enabled') else ' '} | "
            f"{rule} | {text} |"
        )
    return "\n".join(lines) + "\n"
