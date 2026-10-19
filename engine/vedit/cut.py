"""Mechanical cutting: word records in, segment dicts out.

Layering, and the order is load-bearing:

    story   ->  which BLOCKS of content survive and in what order (LLM)
    retake  ->  which attempt at a block survives           (n-gram)
    filler  ->  which tokens inside an attempt survive       (tables)
    silence ->  where the dead air between kept tokens goes  (gaps)

Mechanical cutting runs only INSIDE kept story blocks. Running retake and
filler detection over material that is going to be dropped wholesale is wasted
work, and worse, it lets a stutter in a dropped tangent influence the cut
density of the part you keep.

Two invariants this module never breaks:

* **Nothing is deleted.** Every frame of every kept story block is covered by
  exactly one segment. Removed material is a segment with ``enabled: false``, a
  ``reason`` and full ``provenance``; flipping that one boolean restores it
  frame-exactly, because the disabled segment's frame range is literally the
  hole between its neighbours.
* **Tier C is flagged, never removed.** German modal particles carry attitude.
  They ride along in ``provenance.flags`` on the segment that contains them.

Every threshold below comes from CONTRACT section 8, which is research
synthesis, not perceptual testing. ``calibrated: false`` stays in project.json
until these have been checked against a real German fixture.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Sequence

from . import fillers as F
from .timebase import Timebase
from .transcript import LOW_SCORE, flatten_text, gap_before

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, Any] = {
    "language": "de",
    "source_id": "A",

    # --- splice padding (CONTRACT section 8) ---
    "pad_in": 0.08,          # plosive closure before /p t k b d g/
    "pad_out": 0.10,         # /t k s/ releases run past WhisperX 'end'
    "keep_pause": 0.18,      # floor on the breath slot left where dead air was
    "min_gap": 0.55,         # below this, never cut
    "min_removal": 0.20,     # shorter removals are not worth a cut
    "max_cut_density": 1.5,  # one cut per 1.5 s of output; jump-cut fatigue
    "min_clip": 0.35,        # shorter kept clips merge into a neighbour

    # Retained pause by original gap size. Research synthesis, uncalibrated.
    "pause_tiers": [
        [0.55, 1.20, 0.30],
        [1.20, 3.00, 0.45],
        [3.00, None, 0.60],  # usually a retake boundary or notes-checking
    ],
    "review_gap": 3.00,      # gaps above this are flagged for human review

    # WhisperX 'score' is an ALIGNMENT score, never a filler signal. Below this
    # the word's boundaries are unreliable: widen padding, forbid a cut inside.
    "low_score": LOW_SCORE,
    "low_score_pad_extra": 0.06,

    # --- audio splice hygiene; the audio lane consumes these ---
    "crossfade_ms": 20,
    "crossfade_min_ms": 10,
    "crossfade_max_ms": 40,

    # J/L cuts default to 0. On a single-source talking head a J/L cut with no
    # B-roll cover is just visible lip-sync error (CONTRACT section 8).
    "lead_frames": 0,
    "tail_frames": 0,

    "filler": {
        "dur_per_char": 0.12,        # a genuine filled pause is long for its phonemes
        "flank_gap": 0.10,           # a filled pause IS a pause you filled
        "coarticulated_gap": 0.05,   # both sides below this: phonetically fused
        "tier_b_gap": 0.06,
        "tier_b_min_spacing_s": 4.0, # <= 1 tier-B removal per 4 s of output
        "density_window_s": 5.0,
        "density_max_ratio": 0.25,   # above this the speaker was struggling
        "sentence_initial_gap": 0.80,
        "tier_c_gate": True,         # only flag tier C when it looks filler-like
        # {word_index: dB RELATIVE to surrounding speech}, supplied by the audio
        # lane when it has run. A candidate 15 dB down is a breath Whisper heard
        # as "Ähm", not a filled pause, and belongs to the breath handler.
        "energy_db": None,
        "energy_tolerance_db": 6.0,
    },

    "retake": {
        "cue_gap": 0.35,             # candidate interruption point
        "boundary_gap": 0.80,        # counts as a sentence boundary when extending back
        "n_max": 8,
        "n_min": 3,
        "sim_propose": 0.72,
        "sim_auto": 0.85,
        "n_auto": 4,
        # Enumeration guard: after ordinals and function words are stripped, the
        # CONTENT words must match at least as well as the raw n-gram gate. Set
        # lower and "erstens prüfen wir die Bildrate / zweitens prüfen wir die
        # Tonspur" fires as a restart, which is the classic false positive.
        "content_sim_min": 0.72,
        "multitake": False,          # sentence-level TF-IDF pass; propose-only
        "multitake_cosine": 0.80,
        "multitake_window_s": 120.0,
        "multitake_len_ratio": 0.90,
        "multitake_min_sentences": 2,
    },

    "story": {
        "uncovered": "drop",   # words in no block: "drop" | "keep"
        "maybe": "keep",       # decision == "maybe": "keep" (flagged) | "drop"
    },

    # Word indices where the story pass saw a topic change. Retained pause x2.
    "topic_change_indices": [],
}


def default_config(**overrides: Any) -> dict:
    """A deep copy of :data:`DEFAULTS` with ``overrides`` deep-merged in."""
    cfg = copy.deepcopy(DEFAULTS)
    _deep_merge(cfg, overrides)
    return cfg


def _deep_merge(base: dict, overlay: dict) -> dict:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _resolve(cfg: dict | None) -> dict:
    if cfg is None:
        return default_config()
    # A caller-supplied partial config must not KeyError on an unset threshold.
    merged = copy.deepcopy(DEFAULTS)
    _deep_merge(merged, cfg)
    return merged


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #

def _unreliable(word: dict, cfg: dict) -> bool:
    return bool(word.get("interpolated")) or float(word.get("score", 1.0)) < cfg["low_score"]


def _pad(words: Sequence[dict], i: int, cfg: dict, side: str) -> float:
    """Padding at one clip boundary, widened around an unreliable timestamp."""
    base = cfg["pad_in"] if side == "in" else cfg["pad_out"]
    if _unreliable(words[i], cfg):
        base += cfg["low_score_pad_extra"]
    return base


def _clamp_range(rng: Sequence[int], lo: int, hi: int) -> tuple[int, int]:
    a = max(lo, min(int(rng[0]), hi))
    b = max(a, min(int(rng[1]), hi))
    return a, b


def _lcs(a: Sequence[str], b: Sequence[str]) -> int:
    """Longest common SUBSEQUENCE length. O(len(a) * len(b)), n <= 8 here."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(cur[j], prev[j + 1]))
        prev = cur
    return prev[-1]


def _similarity(a: Sequence[str], b: Sequence[str]) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * _lcs(a, b) / (len(a) + len(b))


# --------------------------------------------------------------------------- #
# 1. SILENCE
# --------------------------------------------------------------------------- #

def _pause_tier(gap: float, cfg: dict) -> tuple[float, str]:
    for idx, (lo, hi, base) in enumerate(cfg["pause_tiers"], start=1):
        if gap >= lo and (hi is None or gap < hi):
            return float(base), f"gap_tier_{idx}"
    return float(cfg["pause_tiers"][0][2]), "gap_tier_1"


def _retained_pause(words: Sequence[dict], i: int, gap: float, cfg: dict) -> tuple[float, dict]:
    """How much of the gap survives the cut, scaled by grammar.

    A single flat threshold makes a clause boundary and a topic change sound
    identical, which is exactly the prosody a listener uses to find the syntax.
    So the base tier is modified by the punctuation of the word BEFORE the gap
    (CONTRACT section 8).
    """
    base, tier_rule = _pause_tier(gap, cfg)
    punct = F.punctuation_class(str(words[i].get("word", "")))
    topic = set(int(x) for x in cfg.get("topic_change_indices") or ())

    if (i + 1) in topic:
        factor, floor, why = 2.0, 0.60, "topic_change"
    elif punct == "sentence":
        factor, floor, why = 1.5, 0.40, "sentence_end"
    elif punct == "clause":
        factor, floor, why = 1.0, 0.0, "clause_end"
    else:
        factor, floor, why = 0.7, 0.20, "mid_clause"

    retained = max(base * factor, floor, cfg["keep_pause"])
    # Never invent time that was not in the recording.
    retained = min(retained, gap)
    return retained, {"tier": tier_rule, "punct": why, "factor": factor, "base": base}


def _silence_confidence(gap: float, cfg: dict) -> float:
    # Heuristic, uncalibrated: a longer gap is more certainly a real pause and
    # less certainly a phonetic artefact. Above review_gap it drops again,
    # because that length is usually a retake boundary, not a beat.
    if gap >= cfg["review_gap"]:
        return 0.90
    if gap >= cfg["pause_tiers"][1][0]:
        return 0.97
    return 0.85


def _candidate_cuts(words: Sequence[dict], lo: int, hi: int, cfg: dict) -> list[dict]:
    """Every gap inside ``[lo, hi)`` that is worth considering as a cut."""
    cuts: list[dict] = []
    consumed = cfg["pad_in"] + cfg["pad_out"]
    for i in range(lo, hi - 1):
        gap = gap_before(words, i + 1)
        if gap < cfg["min_gap"]:
            continue
        retained, detail = _retained_pause(words, i, gap, cfg)
        removal = gap - retained - consumed
        cuts.append({
            "i": i,                       # cut sits between word i and word i+1
            "gap": gap,
            "retained": retained,
            "removal": removal,
            "rule": detail["tier"],
            "detail": detail,
            "confidence": _silence_confidence(gap, cfg),
            "review": gap >= cfg["review_gap"],
        })
    return cuts


def _clip_duration(words: Sequence[dict], a: int, b: int,
                   head_retained: float, tail_retained: float, cfg: dict) -> float:
    """Output duration of the kept clip spanning words ``[a, b]`` inclusive."""
    t_in = float(words[a]["start"]) - _pad(words, a, cfg, "in") - head_retained / 2.0
    t_out = float(words[b]["end"]) + _pad(words, b, cfg, "out") + tail_retained / 2.0
    return max(0.0, t_out - max(0.0, t_in))


def _post_passes(words: Sequence[dict], lo: int, hi: int,
                 cuts: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """The three post-passes, in the contracted order.

    1. min_removal  -- a cut that saves less than min_removal is not worth the
       splice; the pause stays as recorded.
    2. density      -- two cuts closer than max_cut_density on the OUTPUT
       timeline read as jump-cut fatigue; the larger removal wins.
    3. min_clip     -- a kept clip shorter than min_clip is a visual glitch on a
       talking head; it merges into a neighbour by dropping the cut that
       isolated it.

    Pass 2 subsumes pass 3 for clips bounded by two cuts (1.5 s > 0.35 s), but
    pass 3 is not redundant: it is the only pass that sees the FIRST and LAST
    clip of a run, whose outer boundary is a story or removal edge rather than a
    cut.
    """
    dropped: list[dict] = []
    kept = sorted(cuts, key=lambda c: c["i"])

    # -- 1. min_removal -----------------------------------------------------
    survivors = []
    for cut in kept:
        if cut["removal"] < cfg["min_removal"]:
            cut["dropped_by"] = "min_removal"
            dropped.append(cut)
        else:
            survivors.append(cut)
    kept = survivors

    def clip_bounds() -> list[tuple[int, int, dict | None, dict | None]]:
        out: list[tuple[int, int, dict | None, dict | None]] = []
        start = lo
        prev_cut: dict | None = None
        for cut in kept:
            out.append((start, cut["i"], prev_cut, cut))
            start = cut["i"] + 1
            prev_cut = cut
        out.append((start, hi - 1, prev_cut, None))
        return out

    # -- 2. density merge ---------------------------------------------------
    changed = True
    while changed and len(kept) >= 2:
        changed = False
        for a, b, before, after in clip_bounds():
            if before is None or after is None or b < a:
                continue  # interior clips only; run edges belong to pass 3
            dur = _clip_duration(words, a, b, before["retained"], after["retained"], cfg)
            if dur < cfg["max_cut_density"]:
                loser = before if before["removal"] <= after["removal"] else after
                loser["dropped_by"] = "max_cut_density"
                dropped.append(loser)
                kept.remove(loser)
                changed = True
                break

    # -- 3. min_clip merge --------------------------------------------------
    changed = True
    while changed and kept:
        changed = False
        for a, b, before, after in clip_bounds():
            if b < a:
                continue
            dur = _clip_duration(
                words, a, b,
                before["retained"] if before else 0.0,
                after["retained"] if after else 0.0,
                cfg,
            )
            if dur >= cfg["min_clip"]:
                continue
            candidates = [c for c in (before, after) if c is not None]
            if not candidates:
                continue
            loser = min(candidates, key=lambda c: c["removal"])
            loser["dropped_by"] = "min_clip"
            dropped.append(loser)
            kept.remove(loser)
            changed = True
            break

    return kept, dropped


def _clips_for_run(words: Sequence[dict], lo: int, hi: int, tb: Timebase, cfg: dict,
                   *, limit_in: float | None = None, limit_out: float | None = None,
                   forced_splits: dict[int, dict] | None = None) -> list[dict]:
    """One keep-run -> frame-snapped clips, with the holes between them described.

    ``limit_in`` / ``limit_out`` are hard time limits for padding at the run
    edges. They matter when the neighbour is a REMOVED filler: padding into it
    would leave the ``ähm`` audible on the tail of the previous clip.

    ``forced_splits`` are word indices where the clip must break even though
    there is no pause -- a proposed retake is split out so that rejecting it is
    one boolean, not a re-cut. A forced split is frame-contiguous by
    construction: the outgoing clip's exclusive OUT frame is the incoming
    clip's inclusive IN frame, so nothing is lost or duplicated while both are
    enabled.
    """
    forced_splits = forced_splits or {}
    if hi <= lo:
        return []

    cuts, dropped = _post_passes(words, lo, hi, _candidate_cuts(words, lo, hi, cfg), cfg)
    cut_at = {c["i"]: c for c in cuts}

    boundaries = sorted(set(cut_at) | {i - 1 for i in forced_splits if lo < i < hi})

    clips: list[dict] = []
    start = lo
    for b in boundaries + [hi - 1]:
        if b < start:
            continue
        clips.append({"a": start, "b": b, "cut_after": cut_at.get(b),
                      "split_after": b + 1 if (b + 1) in forced_splits and b + 1 < hi else None})
        start = b + 1

    for idx, clip in enumerate(clips):
        prev = clips[idx - 1] if idx else None
        head_retained = prev["cut_after"]["retained"] if prev and prev["cut_after"] else 0.0
        tail_retained = clip["cut_after"]["retained"] if clip["cut_after"] else 0.0

        t_in = float(words[clip["a"]]["start"]) - _pad(words, clip["a"], cfg, "in") - head_retained / 2.0
        t_out = float(words[clip["b"]]["end"]) + _pad(words, clip["b"], cfg, "out") + tail_retained / 2.0

        if idx == 0 and limit_in is not None:
            t_in = max(t_in, limit_in)
        if idx == len(clips) - 1 and limit_out is not None:
            t_out = min(t_out, limit_out)
        t_in = max(0.0, t_in)

        in_frame, out_frame = tb.snap_pair(t_in, t_out)
        clip["t_in"], clip["t_out"] = t_in, t_out
        clip["in_frame"], clip["out_frame"] = in_frame, out_frame
        clip["dropped_cuts"] = dropped if idx == 0 else []

    # A forced split must be frame-contiguous. Snap both sides to one boundary
    # frame at the midpoint of the two words, overriding the padded values.
    for idx in range(len(clips) - 1):
        left, right = clips[idx], clips[idx + 1]
        if left["split_after"] is None:
            continue
        mid = (float(words[left["b"]]["end"]) + float(words[right["a"]]["start"])) / 2.0
        boundary = tb.snap_in(mid)
        boundary = max(left["in_frame"] + 1, boundary)
        boundary = min(boundary, right["out_frame"] - 1)
        left["out_frame"] = boundary
        right["in_frame"] = boundary

    return clips


def detect_silence_cuts(words: Sequence[dict], tb: Timebase, cfg: dict | None = None) -> list[dict]:
    """Gaps between words -> kept clips and the disabled gaps between them.

    Returned segments cover ``words`` completely: every frame is either a kept
    clip or a disabled ``silence_gap``. Used standalone this is the whole rough
    cut for a single take; :func:`build_segments` calls the same machinery per
    story block.
    """
    cfg = _resolve(cfg)
    if not words:
        return []
    clips = _clips_for_run(words, 0, len(words), tb, cfg)
    segments = _emit(words, clips, tb, cfg, block=None, flags_by_word={})
    return _number(segments)


# --------------------------------------------------------------------------- #
# 2. FILLERS
# --------------------------------------------------------------------------- #

_CONNECTORS = {
    "de": {"und", "oder", "aber", "denn", "sondern", "weil", "dass", "wenn", "also"},
    "en": {"and", "or", "but", "because", "that", "if", "so"},
}


def detect_fillers(words: Sequence[dict], cfg: dict | None = None) -> list[dict]:
    """Tier A/B/C classification over the flattened word list.

    Signal order is fixed (CONTRACT section 8): lexical match, then
    ``duration / char >= 0.12 s``, then a flanking gap of at least 0.10 s, then
    an energy sanity check if the audio lane supplied one.

    ``score`` is deliberately absent from that list. It is a wav2vec2 alignment
    score: low means "the audio does not match these letters", which correlates
    with rare words and proper nouns -- the exact opposite of a filler. It is
    used here only to mark a timestamp unreliable.

    Returns one record per hit, with ``action`` in ``remove`` | ``propose`` |
    ``flag``. Tier C is always ``flag``.
    """
    cfg = _resolve(cfg)
    fcfg = cfg["filler"]
    lang = cfg["language"]
    tokens = [F.normalize_token(str(w.get("word", ""))) for w in words]
    energy = fcfg.get("energy_db") or {}

    hits: list[dict] = []
    i = 0
    while i < len(words):
        if not tokens[i]:
            i += 1
            continue
        found = F.lookup(tokens, i, lang)
        if found is None:
            i += 1
            continue
        entry, n = found
        j = i + n

        phrase_chars = max(1, len(entry.phrase.replace(" ", "")))
        dur = float(words[j - 1]["end"]) - float(words[i]["start"])
        dur = max(0.0, dur)
        dpc = dur / phrase_chars
        g_before = gap_before(words, i)
        g_after = gap_before(words, j) if j < len(words) else 0.0
        prev_punct = F.punctuation_class(str(words[i - 1].get("word", ""))) if i else "sentence"
        sentence_initial = i == 0 or prev_punct == "sentence"
        e_db = energy.get(i, energy.get(str(i)))

        signals = {
            "lexical": True,
            "ambiguous": entry.ambiguous,
            "dur_s": round(dur, 4),
            "dur_per_char": round(dpc, 4),
            "dur_per_char_ok": dpc >= fcfg["dur_per_char"],
            "gap_before": round(g_before, 4),
            "gap_after": round(g_after, 4),
            "gap_ok": max(g_before, g_after) >= fcfg["flank_gap"],
            "coarticulated": (g_before < fcfg["coarticulated_gap"]
                              and g_after < fcfg["coarticulated_gap"]),
            "sentence_initial": sentence_initial,
            "energy_db": e_db,
            "energy_ok": None if e_db is None else abs(float(e_db)) <= fcfg["energy_tolerance_db"],
            "unreliable_timestamp": any(_unreliable(words[k], cfg) for k in range(i, j)),
        }

        hit = {
            "word_range": [i, j],
            "text": flatten_text(words[i:j]),
            "normalized": entry.phrase,
            "tier": entry.tier,
            "language": lang,
            "rule": f"tier_{entry.tier.lower()}_lexical",
            "signals": signals,
            "blocks": [],
            "note": entry.note,
        }

        if entry.tier == "A":
            _gate_tier_a(hit, entry, signals, fcfg)
        elif entry.tier == "B":
            _gate_tier_b(hit, entry, signals, words, tokens, i, j, lang, fcfg)
        else:
            _gate_tier_c(hit, signals, fcfg)

        if hit["action"] is not None:
            hits.append(hit)
            # A tier-C particle can sit inside a tier-B phrase ("sag ich mal").
            # Advancing past the whole match keeps hits non-overlapping, which
            # the segment builder relies on.
            i = j
        else:
            i += 1

    _rate_limit_tier_b(hits, words, cfg)
    _struggle_density(hits, words, cfg)
    return hits


def _gate_tier_a(hit: dict, entry: F.FillerEntry, sig: dict, fcfg: dict) -> None:
    """Non-lexical filled pause: lexical AND (duration OR flanking gap)."""
    ok = sig["dur_per_char_ok"] or sig["gap_ok"]
    if entry.ambiguous:
        # 'um', 'er', 'am', 'eh' are real words in one of the two languages.
        # The lexical hit alone proves nothing; the duration test must carry it.
        ok = sig["dur_per_char_ok"]
    if not ok:
        hit["action"] = "flag"
        hit["blocks"].append("weak_signal")
        hit["confidence"] = 0.4
        return
    if sig["coarticulated"]:
        # Both gaps under 50 ms: the filler is phonetically fused with its
        # neighbours. Excising it chops the tail of the previous word and the
        # head of the next, which is far more audible than the filler.
        hit["action"] = "flag"
        hit["blocks"].append("coarticulated")
        hit["confidence"] = 0.55
        return
    if sig["sentence_initial"] and sig["gap_before"] >= fcfg["sentence_initial_gap"]:
        # Sentence-initial after a long pause: the filler is the turn-taking and
        # topic-shift cue. Remove it and the next sentence starts abruptly on
        # the wrong pitch.
        hit["action"] = "propose"
        hit["blocks"].append("sentence_initial_cue")
        hit["confidence"] = 0.6
        return
    if sig["energy_ok"] is False:
        # 15 dB down from surrounding speech is a breath Whisper heard as 'Ähm'.
        hit["action"] = "propose"
        hit["blocks"].append("energy_outlier")
        hit["confidence"] = 0.5
        return
    hit["action"] = "remove"
    hit["confidence"] = 0.95 if (sig["dur_per_char_ok"] and sig["gap_ok"]) else 0.85


def _gate_tier_b(hit: dict, entry: F.FillerEntry, sig: dict, words: Sequence[dict],
                 tokens: Sequence[str], i: int, j: int, lang: str, fcfg: dict) -> None:
    """Lexical filler: auto-remove only behind all three guards."""
    if entry.tag_only:
        # 'ne', 'oder', 'right' are fillers only as a sentence-final tag.
        tail = F.punctuation_class(str(words[j - 1].get("word", "")))
        at_end = j >= len(words) or tail in ("sentence", "clause")
        if not at_end:
            hit["action"] = None
            return
    if max(sig["gap_before"], sig["gap_after"]) < fcfg["tier_b_gap"]:
        hit["action"] = "flag"
        hit["blocks"].append("no_flanking_gap")
        hit["confidence"] = 0.4
        return
    prev_tok = tokens[i - 1] if i else ""
    next_tok = tokens[j] if j < len(tokens) else ""
    if prev_tok in _CONNECTORS.get(lang, set()) and next_tok:
        # Removing here would leave "und ... und" or a connector with nothing to
        # connect. Grammatical damage is worse than the filler.
        hit["action"] = "propose"
        hit["blocks"].append("dangling_connector")
        hit["confidence"] = 0.55
        return
    if entry.ambiguous and not sig["dur_per_char_ok"]:
        hit["action"] = "propose"
        hit["blocks"].append("ambiguous_lexeme")
        hit["confidence"] = 0.6
        return
    hit["action"] = "remove"
    hit["confidence"] = 0.8


def _gate_tier_c(hit: dict, sig: dict, fcfg: dict) -> None:
    """Modal particles. FLAG ONLY -- there is no path from here to removal.

    Gated so the report is readable: every German 'ja', 'so' and 'mal' is a
    tier-C lexeme, but only the ones that also look filler-like (long for their
    phonemes, flanked by a pause, or sentence-initial) are worth a human's
    attention.
    """
    hit["confidence"] = 0.5
    if not fcfg["tier_c_gate"]:
        hit["action"] = "flag"
        return
    if sig["dur_per_char_ok"] or sig["gap_ok"] or sig["sentence_initial"]:
        hit["action"] = "flag"
        return
    hit["action"] = None


def _rate_limit_tier_b(hits: list[dict], words: Sequence[dict], cfg: dict) -> None:
    """At most one tier-B removal per 4 s. Denser than that reads as scrubbed."""
    spacing = cfg["filler"]["tier_b_min_spacing_s"]
    last: float | None = None
    for hit in hits:
        if hit["tier"] != "B" or hit["action"] != "remove":
            continue
        t = float(words[hit["word_range"][0]]["start"])
        if last is not None and t - last < spacing:
            hit["action"] = "propose"
            hit["blocks"].append("tier_b_rate_limit")
            continue
        last = t


def _struggle_density(hits: list[dict], words: Sequence[dict], cfg: dict) -> None:
    """Downgrade removals in any window where the speaker was clearly struggling.

    Removing more than 25 % of the tokens in a 5 s window does not rescue the
    take; it produces an unnaturally dense stretch that reads as scrubbed. The
    right answer there is a retake or B-roll cover, so the whole window is
    handed to a human instead.
    """
    window = cfg["filler"]["density_window_s"]
    limit = cfg["filler"]["density_max_ratio"]
    removals = [h for h in hits if h["action"] == "remove"]
    if not removals:
        return
    flagged: list[dict] = []
    for hit in removals:
        t0 = float(words[hit["word_range"][0]]["start"])
        lo, hi = t0 - window / 2.0, t0 + window / 2.0
        total = sum(1 for w in words if lo <= float(w["start"]) < hi)
        removed = sum(
            h["word_range"][1] - h["word_range"][0]
            for h in removals
            if lo <= float(words[h["word_range"][0]]["start"]) < hi
        )
        if total and removed / total > limit:
            flagged.append(hit)
    for hit in flagged:
        hit["action"] = "propose"
        hit["blocks"].append("struggle_density")


# --------------------------------------------------------------------------- #
# 3. RETAKES
# --------------------------------------------------------------------------- #

def detect_retakes(words: Sequence[dict], cfg: dict | None = None) -> list[dict]:
    """Descending n-gram restart detection.

    The algorithm, in order:

    0. Build a comparison view: normalized tokens with tier-A filled pauses
       removed but index-mapped back to the originals. The pause is the
       interruption CUE, not part of either attempt.
    1. Candidate interruption points only where there is a cue -- a gap of at
       least 0.35 s, a filled pause, or an explicit restart marker. This single
       requirement is what stops rhetorical anaphora ("Wir bauen. Wir bauen
       weiter."), enumerations and corrected facts from firing: without an
       interruption, a repeat is rhetoric, not a restart.
    2. Descending n from 8 to 3, LCS similarity ``2*LCS/(|A|+|B|)``, largest n
       that fires wins.
    3. Extend backwards to a clean sentence boundary. This is the step that
       separates "removes the stutter" from "removes the bad take": speakers
       restart the whole clause, not just the matched n-gram.
    4. Gates: ``sim >= 0.85 and n >= 4`` auto-applies; ``0.72 <= sim < 0.85``
       or ``n == 3`` proposes; a marker with no n-gram match proposes.

    Text-only disfluency detection tops out around F 86-91 %, so roughly one
    miss or false fire per ten. That is why nothing here deletes: an
    auto-applied retake is a segment with ``enabled: false`` and an
    ``alternatives`` pointer to the take that replaced it.
    """
    cfg = _resolve(cfg)
    rc = cfg["retake"]
    lang = cfg["language"]
    if len(words) < 4:
        return []

    toks = [F.normalize_token(str(w.get("word", ""))) for w in words]
    cmp_idx = [i for i, t in enumerate(toks) if t and not F.is_tier_a(t, lang)]
    cmp = [toks[i] for i in cmp_idx]
    if len(cmp) < 4:
        return []

    stop = lambda t: F.is_stopword(t, lang)  # noqa: E731 - local alias, read once
    found: list[dict] = []

    for k in range(1, len(cmp)):
        orig_prev, orig_k = cmp_idx[k - 1], cmp_idx[k]

        max_gap = max((gap_before(words, x) for x in range(orig_prev + 1, orig_k + 1)), default=0.0)
        filled = any(F.is_tier_a(toks[x], lang) for x in range(orig_prev + 1, orig_k))
        marker = F.lookup_restart_marker(cmp, k, lang)
        m = marker[1] if marker else 0

        if marker:
            cue = "marker"
        elif filled:
            cue = "filled_pause"
        elif max_gap >= rc["cue_gap"]:
            cue = "gap"
        else:
            continue  # no interruption cue: this is rhetoric, not a restart

        if k + m >= len(cmp):
            continue
        # Diarization guard: never match a reparandum against another speaker.
        if words[orig_prev].get("speaker") != words[cmp_idx[k + m]].get("speaker"):
            continue

        best: dict | None = None
        for n in range(rc["n_max"], rc["n_min"] - 1, -1):
            if k - n < 0 or k + m + n > len(cmp):
                continue
            a_tokens = cmp[k - n:k]
            b_tokens = cmp[k + m:k + m + n]
            sim = _similarity(a_tokens, b_tokens)
            fires = (n >= 4 and sim >= rc["sim_propose"]) or (n == 3 and sim >= 0.999)
            if not fires:
                continue
            # Enumeration guard: "erstens ..., zweitens ..." share their frame.
            # Strip ordinals and function words and require the CONTENT words to
            # carry the match.
            a_content = [t for t in a_tokens if not stop(t)]
            b_content = [t for t in b_tokens if not stop(t)]
            if a_content and b_content:
                content_sim = _similarity(a_content, b_content)
            elif not a_content and not b_content:
                content_sim = sim
            else:
                content_sim = 0.0
            if content_sim < rc["content_sim_min"]:
                continue
            best = {"n": n, "sim": sim, "content_sim": content_sim}
            break  # descending: the first fire is the largest n

        if best is None:
            if cue != "marker":
                continue
            found.append(_marker_only(words, cmp_idx, toks, k, m, marker, cfg))
            continue

        n = best["n"]
        start = _extend_back(words, toks, cmp_idx[k - n], cmp_idx[k], cfg)
        rep_lo = cmp_idx[k + m]
        rep_hi = cmp_idx[min(k + m + n, len(cmp)) - 1] + 1

        confidence = min(0.99, best["sim"] + 0.02 * (n - 3) + (0.06 if marker else 0.0))
        auto = best["sim"] >= rc["sim_auto"] and n >= rc["n_auto"]
        found.append({
            "kind": "retake",
            "reparandum": [start, rep_lo],
            "repair": [rep_lo, rep_hi],
            "matched_n": n,
            "similarity": round(best["sim"], 4),
            "content_similarity": round(best["content_sim"], 4),
            "cue": cue,
            "marker": marker[0] if marker else None,
            "action": "auto" if auto else "propose",
            "confidence": round(confidence, 4),
            "rule": "ngram_restart",
            "reparandum_text": flatten_text(words[start:rep_lo]),
            "repair_text": flatten_text(words[rep_lo:rep_hi]),
            "guards": ["interruption_cue", "content_words", "same_speaker"],
        })

    hits = _dedupe_overlaps(found)
    if rc["multitake"]:
        hits.extend(detect_multitake_runs(words, cfg))
        hits = _dedupe_overlaps(hits)
    return hits


def _marker_only(words: Sequence[dict], cmp_idx: Sequence[int], toks: Sequence[str],
                 k: int, m: int, marker: tuple[str, int], cfg: dict) -> dict:
    """A restart marker with no n-gram match still proposes, with a wide window."""
    rep_lo = cmp_idx[k + m]
    start = _extend_back(words, toks, cmp_idx[k], cmp_idx[k], cfg)
    rep_hi = rep_lo
    while rep_hi < len(words) and not F.is_sentence_end(str(words[rep_hi].get("word", ""))):
        rep_hi += 1
    rep_hi = min(len(words), rep_hi + 1)
    return {
        "kind": "retake",
        "reparandum": [start, rep_lo],
        "repair": [rep_lo, rep_hi],
        "matched_n": 0,
        "similarity": None,
        "content_similarity": None,
        "cue": "marker",
        "marker": marker[0],
        "action": "propose",
        "confidence": 0.6,
        "rule": "restart_marker",
        "reparandum_text": flatten_text(words[start:rep_lo]),
        "repair_text": flatten_text(words[rep_lo:rep_hi]),
        "guards": ["interruption_cue"],
    }


def _is_boundary(words: Sequence[dict], a: int, boundary_gap: float) -> bool:
    if a <= 0:
        return True
    if F.is_sentence_end(str(words[a - 1].get("word", ""))):
        return True
    if gap_before(words, a) >= boundary_gap:
        return True
    return words[a].get("speaker") != words[a - 1].get("speaker")


def _extend_back(words: Sequence[dict], toks: Sequence[str], i: int, p: int,
                 cfg: dict, floor: int = 0) -> int:
    """Clean start of the utterance the reparandum belongs to.

    Speakers restart the whole clause, not just the matched n-gram, so the
    default direction is backwards. But the window can also OVER-reach: at
    ``n = 8`` the head of ``A`` routinely lands in the previous sentence, and
    blindly walking left from there would disable a sentence the speaker never
    retook. So a boundary found INSIDE ``[i, p)`` wins first, and only if there
    is none does this walk left.
    """
    boundary_gap = cfg["retake"]["boundary_gap"]
    inner = [a for a in range(max(i, floor) + 1, p) if _is_boundary(words, a, boundary_gap)]
    if inner:
        return inner[-1]
    a = max(i, floor)
    while a > floor and not _is_boundary(words, a, boundary_gap):
        a -= 1
    return a


def _dedupe_overlaps(hits: list[dict]) -> list[dict]:
    """Keep the most confident detection when two reparanda overlap."""
    ordered = sorted(hits, key=lambda h: (-float(h.get("confidence") or 0.0), h["reparandum"][0]))
    kept: list[dict] = []
    for hit in ordered:
        a, b = hit["reparandum"]
        if any(not (b <= x or a >= y) for x, y in (h["reparandum"] for h in kept)):
            continue
        kept.append(hit)
    kept.sort(key=lambda h: h["reparandum"][0])
    return kept


# --- the multi-take case (step 5) ------------------------------------------- #

def _sentences(words: Sequence[dict]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = 0
    for i, w in enumerate(words):
        if F.is_sentence_end(str(w.get("word", ""))):
            out.append((start, i + 1))
            start = i + 1
    if start < len(words):
        out.append((start, len(words)))
    return out


def _tfidf(docs: Sequence[Sequence[str]]) -> list[dict[str, float]]:
    df: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    n = max(1, len(docs))
    vectors: list[dict[str, float]] = []
    for doc in docs:
        tf: dict[str, int] = {}
        for term in doc:
            tf[term] = tf.get(term, 0) + 1
        # Smoothed idf (the +1 is scikit-learn's smooth_idf convention): with a
        # short transcript a term can appear in EVERY sentence, and unsmoothed
        # idf would zero exactly the terms that prove two takes are the same.
        vec = {t: (c / len(doc)) * (math.log((1 + n) / (1 + df[t])) + 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def detect_multitake_runs(words: Sequence[dict], cfg: dict | None = None) -> list[dict]:
    """Sentence-level near-duplicate detection for whole re-recorded blocks.

    N-gram matching cannot span a 30-second block done three times. This finds
    sentences whose best match is a LATER sentence and proposes dropping the
    earlier one. Always ``propose``, never ``auto``: the guards are statistical,
    not structural.
    """
    cfg = _resolve(cfg)
    rc = cfg["retake"]
    lang = cfg["language"]
    sents = _sentences(words)
    if len(sents) < 2 * rc["multitake_min_sentences"]:
        return []

    docs: list[list[str]] = []
    for a, b in sents:
        toks = [F.normalize_token(str(w.get("word", ""))) for w in words[a:b]]
        docs.append([t for t in toks if t and not F.is_stopword(t, lang) and not F.is_tier_a(t, lang)]
                    or ["\x00empty"])
    vecs = _tfidf(docs)

    hits: list[dict] = []
    for i in range(len(sents)):
        best_j, best_c = -1, 0.0
        for j in range(i + 1, len(sents)):
            dt = float(words[sents[j][0]]["start"]) - float(words[sents[i][0]]["start"])
            if dt > rc["multitake_window_s"]:
                break
            c = _cosine(vecs[i], vecs[j])
            if c > best_c:
                best_j, best_c = j, c
        if best_j < 0 or best_c < rc["multitake_cosine"]:
            continue
        li = sents[i][1] - sents[i][0]
        lj = sents[best_j][1] - sents[best_j][0]
        if lj < rc["multitake_len_ratio"] * li:
            continue  # do not drop a full explanation for a shorter summary
        hits.append({
            "kind": "retake",
            "reparandum": [sents[i][0], sents[i][1]],
            "repair": [sents[best_j][0], sents[best_j][1]],
            "matched_n": 0,
            "similarity": round(best_c, 4),
            "content_similarity": round(best_c, 4),
            "cue": "sentence_similarity",
            "marker": None,
            "action": "propose",
            "confidence": round(min(0.85, best_c), 4),
            "rule": "multitake_tfidf",
            "reparandum_text": flatten_text(words[sents[i][0]:sents[i][1]]),
            "repair_text": flatten_text(words[sents[best_j][0]:sents[best_j][1]]),
            "guards": ["window_120s", "length_ratio"],
        })
    return hits


# --------------------------------------------------------------------------- #
# 4. STORY
# --------------------------------------------------------------------------- #

def apply_story(words: Sequence[dict], story: dict | None,
                cfg: dict | None = None) -> list[dict]:
    """``project.json.story`` -> block plans in OUTPUT order.

    Content selection happens before mechanical cutting: which topics survive,
    in what order, and where the hook is. Mechanical cutting then runs only
    inside the kept blocks.

    Ordering rules:

    * kept blocks are emitted in ``order``, which may differ from recording
      order -- that is the point of the pass;
    * a hook is pulled to the front and subtracted from whichever block it was
      recorded inside, so no word is emitted twice;
    * dropped blocks are emitted disabled, placed immediately after the kept
      block that precedes them in RECORDING order, so flipping ``enabled`` puts
      them back somewhere a human expects rather than at the end of the video.
    """
    cfg = _resolve(cfg)
    n = len(words)
    scfg = cfg["story"]

    if not story or not story.get("blocks"):
        return [{
            "block_id": "blk_all", "word_range": [0, n], "label": "(no story pass)",
            "decision": "keep", "order": 0, "enabled": True, "reason": None,
            "rationale": None,
            "provenance": {"stage": "content", "rule": "implicit_all", "applied_by": "auto",
                           "confidence": 1.0},
        }]

    raw: list[dict] = []
    for idx, blk in enumerate(story.get("blocks") or []):
        a, b = _clamp_range(blk.get("word_range") or [0, 0], 0, n)
        if b <= a:
            continue
        decision = str(blk.get("decision", "keep"))
        raw.append({
            "block_id": str(blk.get("id") or f"blk_{idx:02d}"),
            "word_range": [a, b],
            "label": blk.get("label"),
            "decision": decision,
            "order": int(blk["order"]) if blk.get("order") is not None else idx,
            "rationale": blk.get("rationale"),
        })

    hook = story.get("hook") or None
    hook_range: tuple[int, int] | None = None
    if hook and hook.get("word_range"):
        ha, hb = _clamp_range(hook["word_range"], 0, n)
        if hb > ha:
            hook_range = (ha, hb)
            raw = _subtract_from_blocks(raw, hook_range)

    covered = sorted((b["word_range"][0], b["word_range"][1]) for b in raw)
    if hook_range:
        covered = sorted(covered + [hook_range])

    plans: list[dict] = []
    if hook_range:
        plans.append({
            "block_id": "blk_hook", "word_range": [hook_range[0], hook_range[1]],
            "label": hook.get("note") or "hook", "decision": "keep",
            "order": -1, "enabled": True, "reason": None,
            "rationale": hook.get("note"),
            "provenance": {"stage": "content", "rule": "hook_pulled_forward",
                           "applied_by": "llm", "confidence": 1.0},
        })

    for blk in raw:
        decision = blk["decision"]
        if decision == "drop":
            enabled, reason, rule = False, "story_drop", "block_drop"
        elif decision == "maybe":
            keep = scfg["maybe"] == "keep"
            enabled = keep
            reason = None if keep else "story_maybe_drop"
            rule = "block_maybe"
        else:
            enabled, reason, rule = True, None, "block_keep"
        plans.append({
            "block_id": blk["block_id"], "word_range": blk["word_range"],
            "label": blk["label"], "decision": decision, "order": blk["order"],
            "enabled": enabled, "reason": reason, "rationale": blk["rationale"],
            "provenance": {"stage": "content", "rule": rule, "applied_by": "llm",
                           "confidence": 1.0,
                           "flags": ["story_maybe"] if decision == "maybe" else []},
        })

    for a, b in _uncovered(covered, n):
        keep = scfg["uncovered"] == "keep"
        plans.append({
            "block_id": f"blk_gap_{a:04d}", "word_range": [a, b],
            "label": "(not covered by the story pass)", "decision": "drop" if not keep else "keep",
            "order": 10_000 + a, "enabled": keep,
            "reason": None if keep else "story_uncovered", "rationale": None,
            "provenance": {"stage": "content", "rule": "uncovered", "applied_by": "auto",
                           "confidence": 0.5},
        })

    return _order_blocks(plans)


def _subtract_from_blocks(blocks: list[dict], hole: tuple[int, int]) -> list[dict]:
    out: list[dict] = []
    ha, hb = hole
    for blk in blocks:
        a, b = blk["word_range"]
        if hb <= a or ha >= b:
            out.append(blk)
            continue
        left = (a, min(ha, b))
        right = (max(hb, a), b)
        pieces = [p for p in (left, right) if p[1] > p[0]]
        for k, (pa, pb) in enumerate(pieces):
            piece = dict(blk)
            piece["word_range"] = [pa, pb]
            if len(pieces) > 1:
                piece["block_id"] = f"{blk['block_id']}{chr(ord('a') + k)}"
            out.append(piece)
    return out


def _uncovered(covered: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cursor = 0
    for a, b in covered:
        if a > cursor:
            out.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < n:
        out.append((cursor, n))
    return out


def _order_blocks(plans: list[dict]) -> list[dict]:
    """Kept blocks by ``order``; dropped blocks follow their recording predecessor."""
    kept = sorted([p for p in plans if p["enabled"]], key=lambda p: (p["order"], p["word_range"][0]))
    dropped = sorted([p for p in plans if not p["enabled"]], key=lambda p: p["word_range"][0])
    if not kept:
        return dropped
    out = list(kept)
    for plan in dropped:
        anchor = -1
        for i, k in enumerate(out):
            if k["enabled"] and k["word_range"][0] < plan["word_range"][0]:
                anchor = i
        out.insert(anchor + 1, plan)
    return out


# --------------------------------------------------------------------------- #
# 5. ASSEMBLY
# --------------------------------------------------------------------------- #

def build_segments(words: Sequence[dict], tb: Timebase, cfg: dict | None = None,
                   story: dict | None = None) -> list[dict]:
    """The whole rough cut: story -> retakes -> fillers -> silence -> segments.

    Returns segments in output order, ``order`` assigned over enabled AND
    disabled alike so that re-enabling one puts it back in the right place.
    """
    cfg = _resolve(cfg)
    if not words:
        return []

    blocks = apply_story(words, story, cfg)
    fillers = detect_fillers(words, cfg)
    retakes = detect_retakes(words, cfg)

    removals = _removals(fillers, retakes)
    flags_by_word = _flags(fillers, retakes, words, cfg)

    segments: list[dict] = []
    for block in blocks:
        a, b = block["word_range"]
        if not block["enabled"]:
            segments.append(_block_segment(words, block, tb, cfg))
            continue
        segments.extend(_segments_for_block(words, a, b, tb, cfg, block, removals, flags_by_word))

    return _number(segments)


def _removals(fillers: Sequence[dict], retakes: Sequence[dict]) -> list[dict]:
    out: list[dict] = []
    for hit in fillers:
        if hit["action"] == "remove":
            out.append({"range": list(hit["word_range"]), "kind": "filler", "detail": hit})
    for hit in retakes:
        if hit["action"] == "auto":
            out.append({"range": list(hit["reparandum"]), "kind": "retake", "detail": hit})
    out.sort(key=lambda r: (r["range"][0], r["range"][1]))

    merged: list[dict] = []
    for item in out:
        if merged and item["range"][0] < merged[-1]["range"][1]:
            # A filler inside a reparandum is already covered by it. Widen the
            # existing removal and keep both details for provenance.
            prev = merged[-1]
            prev["range"][1] = max(prev["range"][1], item["range"][1])
            prev.setdefault("also", []).append(item["detail"])
            continue
        merged.append(item)
    return merged


def _flags(fillers: Sequence[dict], retakes: Sequence[dict],
           words: Sequence[dict], cfg: dict) -> dict[int, list[dict]]:
    """Everything that must ride along on a KEPT segment rather than cut it."""
    out: dict[int, list[dict]] = {}

    def add(index: int, flag: dict) -> None:
        out.setdefault(index, []).append(flag)

    for hit in fillers:
        if hit["action"] in ("flag", "propose"):
            add(hit["word_range"][0], {
                "kind": "filler", "tier": hit["tier"], "action": hit["action"],
                "text": hit["text"], "word_range": list(hit["word_range"]),
                "rule": hit["rule"], "blocks": list(hit["blocks"]),
                "confidence": hit.get("confidence"),
            })
    for hit in retakes:
        if hit["action"] == "propose":
            add(hit["reparandum"][0], {
                "kind": "retake_proposal", "action": "propose",
                "word_range": list(hit["reparandum"]), "repair": list(hit["repair"]),
                "similarity": hit["similarity"], "rule": hit["rule"],
                "confidence": hit.get("confidence"), "cue": hit["cue"],
                "text": hit["reparandum_text"],
            })
    for i, w in enumerate(words):
        if _unreliable(w, cfg):
            add(i, {"kind": "unreliable_timestamp", "action": "flag",
                    "word_range": [i, i + 1], "text": str(w.get("word", "")),
                    "score": w.get("score"), "interpolated": bool(w.get("interpolated"))})
    return out


def _segments_for_block(words: Sequence[dict], lo: int, hi: int, tb: Timebase, cfg: dict,
                        block: dict, removals: Sequence[dict],
                        flags_by_word: dict[int, list[dict]]) -> list[dict]:
    local = [r for r in removals if r["range"][0] >= lo and r["range"][1] <= hi]
    runs, cancelled, active = _keep_runs(words, lo, hi, local, cfg)

    forced = {}
    for index, flags in flags_by_word.items():
        if not (lo <= index < hi):
            continue
        for flag in flags:
            if flag["kind"] == "retake_proposal":
                # Split the proposed reparandum into its own segment so that
                # accepting the proposal is one boolean, not a re-cut.
                a, b = flag["word_range"]
                if lo < a < hi:
                    forced[a] = flag
                if lo < b < hi:
                    forced[b] = flag

    clips: list[dict] = []
    for run_idx, run in enumerate(runs):
        limit_in = run.get("limit_in")
        limit_out = run.get("limit_out")
        run_clips = _clips_for_run(words, run["a"], run["b"], tb, cfg,
                                   limit_in=limit_in, limit_out=limit_out,
                                   forced_splits={k: v for k, v in forced.items()
                                                  if run["a"] < k < run["b"]})
        for clip in run_clips:
            clip["run"] = run_idx
            clip["removal_after"] = None
        if run_clips:
            # Only the LAST clip of a run is followed by the removal; the ones
            # before it are followed by an ordinary silence cut.
            run_clips[-1]["removal_after"] = run.get("removal_after")
        clips.extend(run_clips)

    for item in cancelled:
        detail = item["detail"]
        flags_by_word.setdefault(item["range"][0], []).append({
            "kind": "cancelled_removal", "action": "propose",
            "word_range": list(item["range"]), "rule": "min_clip_merge",
            "text": detail.get("text") or detail.get("reparandum_text", ""),
        })

    # A removal sitting at the very start or end of a block has no neighbouring
    # clip to hang its hole on, so the block's own padded bounds carry it.
    lead = next((r for r in active if r["range"][0] <= lo), None)
    trail = next((r for r in active if r["range"][1] >= hi), None)
    lo_frame, _ = tb.snap_pair(max(0.0, float(words[lo]["start"]) - cfg["pad_in"]),
                               float(words[lo]["end"]))
    _, hi_frame = tb.snap_pair(float(words[hi - 1]["start"]),
                               float(words[hi - 1]["end"]) + cfg["pad_out"])
    return _emit(words, clips, tb, cfg, block=block, flags_by_word=flags_by_word,
                 edges=(lead, trail, lo_frame, hi_frame))


def _keep_runs(words: Sequence[dict], lo: int, hi: int, removals: Sequence[dict],
               cfg: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Complement of the removals inside the block, with short runs merged back.

    A keep-run shorter than ``min_clip`` is a fragment stranded between two
    removals. Cutting to it produces a visual glitch, so the smaller adjacent
    removal is CANCELLED (downgraded to a proposal) and the runs re-splice.
    """
    active = [dict(r, range=list(r["range"])) for r in removals]
    cancelled: list[dict] = []

    while True:
        runs = _complement(lo, hi, active)
        short = None
        for run in runs:
            if run["b"] - run["a"] <= 0:
                continue
            dur = float(words[run["b"] - 1]["end"]) - float(words[run["a"]]["start"])
            if dur < cfg["min_clip"]:
                short = run
                break
        if short is None or not active:
            return runs, cancelled, active
        neighbours = [r for r in active
                      if r["range"][1] == short["a"] or r["range"][0] == short["b"]]
        if not neighbours:
            return runs, cancelled, active
        loser = min(neighbours, key=lambda r: float(words[r["range"][1] - 1]["end"])
                    - float(words[r["range"][0]]["start"]))
        active.remove(loser)
        cancelled.append(loser)


def _complement(lo: int, hi: int, removals: Sequence[dict]) -> list[dict]:
    runs: list[dict] = []
    cursor = lo
    for rem in sorted(removals, key=lambda r: r["range"][0]):
        a, b = rem["range"]
        if a > cursor:
            runs.append({"a": cursor, "b": a, "removal_after": rem})
        cursor = max(cursor, b)
    if cursor < hi:
        runs.append({"a": cursor, "b": hi, "removal_after": None})
    return runs


def _emit(words: Sequence[dict], clips: Sequence[dict], tb: Timebase, cfg: dict,
          block: dict | None, flags_by_word: dict[int, list[dict]],
          edges: tuple[dict | None, dict | None, int, int] | None = None) -> list[dict]:
    """Clips -> segment dicts, with a disabled segment filling every hole.

    Coverage is total by construction: consecutive segments share a frame
    boundary, so the disabled ones are exactly the holes and re-enabling one is
    frame-exact.
    """
    out: list[dict] = []
    block_id = block["block_id"] if block else None
    lead, trail, lo_frame, hi_frame = edges or (None, None, 0, 0)

    if lead is not None and clips and clips[0]["in_frame"] > lo_frame:
        out.append(_hole_segment(words, lead, None, list(lead["range"]),
                                 lo_frame, clips[0]["in_frame"], cfg, block_id))

    for idx, clip in enumerate(clips):
        prev = clips[idx - 1] if idx else None
        if prev is not None:
            hole_in, hole_out = prev["out_frame"], clip["in_frame"]
            word_range = [prev["b"] + 1, clip["a"]]
            if hole_out > hole_in:
                out.append(_hole_segment(words, prev.get("removal_after"),
                                         prev.get("cut_after"), word_range,
                                         hole_in, hole_out, cfg, block_id))
            elif hole_out < hole_in:
                # Padding from both sides overran a very short removal. The
                # removed words must not survive on either tail, so the two
                # clips butt together at the incoming clip's IN frame.
                out[-1]["src_out_frames"] = hole_out
                out[-1]["render"]["frames"] = hole_out - out[-1]["src_in_frames"]

        out.append(_clip_segment(words, clip, cfg, block, flags_by_word))

    if trail is not None and clips and hi_frame > clips[-1]["out_frame"]:
        out.append(_hole_segment(words, trail, None, list(trail["range"]),
                                 clips[-1]["out_frame"], hi_frame, cfg, block_id))
    return out


def _fade_ms(kind: str, retained: float, cfg: dict) -> int:
    """Crossfade at one splice, in ms.

    20 ms default; never longer than half the retained pause; 10 ms when the
    cut lands inside speech, because there is no room tone to fade across.
    """
    if kind in ("filler", "retake"):
        return int(cfg["crossfade_min_ms"])
    ms = cfg["crossfade_ms"]
    if retained > 0:
        ms = min(ms, max(cfg["crossfade_min_ms"], int(retained * 1000 / 2)))
    return int(max(cfg["crossfade_min_ms"], min(cfg["crossfade_max_ms"], ms)))


def _clip_segment(words: Sequence[dict], clip: dict, cfg: dict, block: dict | None,
                  flags_by_word: dict[int, list[dict]]) -> dict:
    a, b = clip["a"], clip["b"] + 1
    cut = clip.get("cut_after")
    removal = clip.get("removal_after")
    kind_after = removal["kind"] if removal else ("silence" if cut else "none")
    retained = cut["retained"] if cut else 0.0

    flags: list[dict] = []
    for i in range(a, b):
        flags.extend(flags_by_word.get(i, ()))

    provenance: dict[str, Any] = {
        "stage": "silence" if cut else "content",
        "rule": cut["rule"] if cut else "keep",
        "confidence": cut["confidence"] if cut else 1.0,
        "applied_by": "auto",
    }
    if cut:
        provenance["original_gap_s"] = round(cut["gap"], 4)
        provenance["retained_pause_s"] = round(cut["retained"], 4)
        provenance["removal_s"] = round(cut["removal"], 4)
        provenance["punct"] = cut["detail"]["punct"]
        if cut["review"]:
            provenance["review"] = "gap over review_gap: often a retake boundary"
    if block:
        provenance["block_id"] = block["block_id"]
        provenance["block_order"] = block["order"]
    if flags:
        provenance["flags"] = flags

    return _segment(
        cfg, clip["in_frame"], clip["out_frame"], True, [a, b],
        flatten_text(words[a:b]), provenance,
        fade_out_ms=_fade_ms(kind_after, retained, cfg),
    )


def _hole_segment(words: Sequence[dict], removal: dict | None, cut: dict | None,
                  word_range: Sequence[int], in_frame: int, out_frame: int,
                  cfg: dict, block_id: str | None) -> dict:
    if removal is not None:
        detail = removal["detail"]
        if removal["kind"] == "filler":
            provenance = {
                "stage": "filler", "rule": detail["rule"], "applied_by": "auto",
                "confidence": detail.get("confidence"), "tier": detail["tier"],
                "text": detail["text"], "signals": detail["signals"],
            }
            reason = f"filler_tier_{detail['tier'].lower()}"
            extra: dict[str, Any] = {}
        else:
            provenance = {
                "stage": "retake", "rule": detail["rule"], "applied_by": "auto",
                "confidence": detail.get("confidence"), "n": detail["matched_n"],
                "similarity": detail["similarity"], "cue": detail["cue"],
            }
            reason = "retake_superseded"
            extra = {"alternatives": [f"words:{detail['repair'][0]}-{detail['repair'][1]}"]}
        if removal.get("also"):
            provenance["also_covered"] = [d.get("rule") for d in removal["also"]]
    elif cut is not None:
        provenance = {
            "stage": "silence", "rule": cut["rule"], "applied_by": "auto",
            "confidence": cut["confidence"], "original_gap_s": round(cut["gap"], 4),
            "retained_pause_s": round(cut["retained"], 4),
            "removal_s": round(cut["removal"], 4), "punct": cut["detail"]["punct"],
        }
        if cut["review"]:
            provenance["review"] = "gap over review_gap: often a retake boundary"
        reason = "silence_gap"
        extra = {}
    else:
        provenance = {"stage": "silence", "rule": "unclassified_hole", "applied_by": "auto",
                      "confidence": 0.5}
        reason = "gap"
        extra = {}

    if block_id:
        provenance["block_id"] = block_id

    seg = _segment(cfg, in_frame, out_frame, False, word_range,
                   flatten_text(words[word_range[0]:word_range[1]]), provenance)
    seg["reason"] = reason
    seg.update(extra)
    return seg


def _block_segment(words: Sequence[dict], block: dict, tb: Timebase, cfg: dict) -> dict:
    """One disabled segment covering a whole story-dropped block."""
    a, b = block["word_range"]
    t_in = max(0.0, float(words[a]["start"]) - cfg["pad_in"])
    t_out = float(words[b - 1]["end"]) + cfg["pad_out"]
    in_frame, out_frame = tb.snap_pair(t_in, t_out)
    provenance = dict(block["provenance"])
    provenance["block_id"] = block["block_id"]
    provenance["block_order"] = block["order"]
    provenance["label"] = block.get("label")
    if block.get("rationale"):
        provenance["rationale"] = block["rationale"]
    seg = _segment(cfg, in_frame, out_frame, False, [a, b], flatten_text(words[a:b]), provenance)
    seg["reason"] = block.get("reason") or "story_drop"
    return seg


def _segment(cfg: dict, in_frame: int, out_frame: int, enabled: bool,
             word_range: Sequence[int], text: str, provenance: dict,
             *, fade_out_ms: int | None = None) -> dict:
    """One segment dict, exactly the CONTRACT section 3 shape."""
    return {
        "id": None,     # assigned by _number() once output order is known
        "source_id": cfg["source_id"],
        "src_in_frames": int(in_frame),
        "src_out_frames": int(out_frame),
        "enabled": bool(enabled),
        "order": None,
        "speed": 1.0,
        "word_range": [int(word_range[0]), int(word_range[1])],
        "text": text,
        "audio": {
            "src_in_frames": None,
            "src_out_frames": None,
            "lead_frames": int(cfg["lead_frames"]),
            "tail_frames": int(cfg["tail_frames"]),
            "fade_in_ms": int(cfg["crossfade_ms"]),
            "fade_out_ms": int(cfg["crossfade_ms"] if fade_out_ms is None else fade_out_ms),
            "zero_cross_offset_samples": 0,
            "gain_db": 0.0,
        },
        "locked": False,
        "provenance": provenance,
        "render": {"cache_key": None, "frames": int(out_frame) - int(in_frame)},
    }


def _number(segments: list[dict]) -> list[dict]:
    """Assign ids and output order over enabled AND disabled segments alike.

    Numbering the disabled ones too is what makes re-enabling a boolean flip:
    the segment already knows where in the output it belongs.
    """
    for i, seg in enumerate(segments):
        seg["order"] = i
        seg["id"] = f"seg_{i:04d}"
        # The incoming fade of a segment must match the outgoing fade of the
        # previous one, or the splice is asymmetric and audible.
        if i:
            seg["audio"]["fade_in_ms"] = segments[i - 1]["audio"]["fade_out_ms"]
    return segments


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #

def cut_report(segments: Sequence[dict], words: Sequence[dict], tb: Timebase) -> dict:
    """Summary numbers for the QC pass and the skill's console output."""
    enabled = [s for s in segments if s["enabled"]]
    disabled = [s for s in segments if not s["enabled"]]
    kept_frames = sum(s["src_out_frames"] - s["src_in_frames"] for s in enabled)
    cut_frames = sum(s["src_out_frames"] - s["src_in_frames"] for s in disabled)
    by_reason: dict[str, int] = {}
    for seg in disabled:
        key = seg.get("reason", "unknown")
        by_reason[key] = by_reason.get(key, 0) + (seg["src_out_frames"] - seg["src_in_frames"])
    flags: dict[str, int] = {}
    for seg in enabled:
        for flag in (seg["provenance"].get("flags") or ()):
            key = f"{flag['kind']}:{flag.get('tier', flag.get('action', ''))}"
            flags[key] = flags.get(key, 0) + 1
    return {
        "segments": len(segments),
        "enabled": len(enabled),
        "disabled": len(disabled),
        "kept_frames": kept_frames,
        "kept_seconds": round(tb.frames_to_seconds(kept_frames), 3),
        "cut_frames": cut_frames,
        "cut_seconds": round(tb.frames_to_seconds(cut_frames), 3),
        "cut_frames_by_reason": dict(sorted(by_reason.items())),
        "flags": dict(sorted(flags.items())),
        "words": len(words),
        "words_kept": sum(s["word_range"][1] - s["word_range"][0] for s in enabled),
    }
