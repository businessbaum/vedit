"""Lexical tables for German and English disfluency, plus the one normalizer.

Three tiers, and only two of them are ever automatic (CONTRACT section 8):

    A  non-lexical filled pauses        auto-remove
    B  lexical fillers                  auto-remove behind guards
    C  modal particles / discourse      FLAG ONLY, never auto-remove

Tier C is the reason this file exists as a table rather than a regex. German
modal particles carry attitude and discourse structure -- ``Das ist halt so``
does not mean ``Das ist so``, and ``Also, fangen wir an`` is a genuine
connector, not noise. Removing them produces text that is grammatical and
wrong. So they are surfaced to a human or an LLM and never touched by the
machine.

Two design rules that are easy to get wrong:

1. **Umlauts are preserved.** Whisper writes them correctly; transliterating
   ``ähm`` to ``aehm`` would break every lookup here and every caption
   downstream. ``str.lower()`` is used rather than ``str.casefold()`` because
   casefold maps ``ß`` to ``ss`` and would turn ``weiß`` into ``weiss``.

2. **Tables are language-scoped.** ``er`` is an English filled pause and a
   German pronoun; ``um`` is an English filled pause and a German preposition;
   ``am`` is Whisper's occasional spelling of ``ähm`` and also a real German
   contraction. Those entries exist but are marked ``ambiguous``, which means a
   lexical hit alone is never enough -- the duration test in cut.py must also
   pass. Without that scoping, a German edit would delete every ``er``.

All thresholds referenced in the docstrings live in cut.py; this module holds
only vocabulary and normalization so both can be reviewed independently.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from itertools import groupby
from typing import Iterable, Sequence

TIERS: tuple[str, str, str] = ("A", "B", "C")

# Longest phrase in any table ("let me try that again" = 5 tokens). Lookup never
# needs to slice further than this.
MAX_PHRASE_TOKENS: int = 5

# Stripped from both ends of a token only, never from the middle: "Frame-Snapping"
# must stay one token, but "Test." and "»Test«" must normalize to "test".
_STRIP_CHARS = " \t\r\n.,!?;:…\"'»«„“”‚‘’()[]{}—–‐-*_/\\"

_SENTENCE_END = ".!?…"
_CLAUSE_END = ",;:"


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

def _collapse_runs(s: str, threshold: int = 3) -> str:
    """Collapse a run of >= ``threshold`` identical characters down to one.

    ``ähhhh`` -> ``äh``, ``ummm`` -> ``um``. The threshold is 3 and not 2 on
    purpose: German is full of meaningful doubled consonants (``immer``,
    ``Schnitt``, ``alle``) and collapsing those would fabricate collisions with
    the tables. Every two-character variant that matters (``ähh``, ``hmm``,
    ``umm``) is registered explicitly instead.
    """
    parts: list[str] = []
    for ch, grp in groupby(s):
        n = sum(1 for _ in grp)
        parts.append(ch if n >= threshold else ch * n)
    return "".join(parts)


def normalize_token(word: str) -> str:
    """One word -> its canonical comparison form.

    NFC, end punctuation stripped, lowercased, repeated-character runs
    collapsed. Returns "" for a token that was pure punctuation, which callers
    treat as "not a word".

    This is the ONLY normalizer in the pipeline. The tables below are built by
    passing their own entries through it, so table and input can never drift.
    """
    s = unicodedata.normalize("NFC", word).strip()
    s = s.strip(_STRIP_CHARS)
    if not s:
        return ""
    # .lower(), not .casefold(): casefold maps ss<-ß and destroys 'weiß'.
    s = unicodedata.normalize("NFC", s.lower())
    return _collapse_runs(s)


def normalize_tokens(words: Iterable[str]) -> list[str]:
    """Vectorized :func:`normalize_token` over raw word strings."""
    return [normalize_token(w) for w in words]


def strip_punctuation(word: str) -> str:
    """End punctuation removed, case and repeats preserved. For display."""
    return unicodedata.normalize("NFC", word).strip().strip(_STRIP_CHARS)


def punctuation_class(word: str) -> str:
    """``"sentence"`` | ``"clause"`` | ``"none"`` for the trailing punctuation.

    Drives the punctuation-scaled retained pause in CONTRACT section 8. Closing
    quotes and brackets are peeled first so ``sagte er."`` still counts as a
    sentence end.
    """
    s = unicodedata.normalize("NFC", word).rstrip(" \t\r\n\"'»«“”’)]}")
    if not s:
        return "none"
    if s[-1] in _SENTENCE_END:
        return "sentence"
    if s[-1] in _CLAUSE_END:
        return "clause"
    return "none"


def is_sentence_end(word: str) -> bool:
    return punctuation_class(word) == "sentence"


# --------------------------------------------------------------------------- #
# table records
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FillerEntry:
    """One vocabulary item in one language."""

    phrase: str        # normalized, single space between tokens
    tokens: int        # token count, so a match can advance the cursor
    tier: str          # "A" | "B" | "C"
    language: str      # "de" | "en"
    ambiguous: bool    # collides with a real word: needs the duration test too
    tag_only: bool     # only a filler in sentence-final tag position ("..., ne?")
    note: str


_TABLE: dict[tuple[str, str], FillerEntry] = {}


def _register(phrase: str, tier: str, language: str, *,
              ambiguous: bool = False, tag_only: bool = False, note: str = "") -> None:
    norm = " ".join(t for t in (normalize_token(p) for p in phrase.split()) if t)
    if not norm:
        return
    key = (language, norm)
    # First registration wins, so a deliberate ambiguous=True entry is not
    # silently overwritten by a plain duplicate later in the list.
    if key in _TABLE:
        return
    _TABLE[key] = FillerEntry(
        phrase=norm, tokens=len(norm.split()), tier=tier, language=language,
        ambiguous=ambiguous, tag_only=tag_only, note=note,
    )


# --- TIER A: non-lexical filled pauses -------------------------------------- #
# Safe by construction: no propositional content. Everything here still has to
# clear the duration or flanking-gap test in cut.py before it is removed.

_TIER_A_DE = [
    "äh", "ähh", "ähhh", "ähm", "ähmm", "ähmmm", "äm", "ämm", "ehm", "ehmm",
    "öh", "öhm", "hm", "hmm", "mh", "mhm", "mmh", "mm", "äöh", "öhh",
]
# Whisper's German spelling drift plus genuine collisions with real words.
_TIER_A_DE_AMBIGUOUS = {
    "eh": "real German adverb ('das ist eh klar')",
    "am": "real contraction ('am Ende'); Whisper sometimes writes 'ähm' as 'Am'",
    "um": "real German preposition ('um zehn Uhr')",
    "er": "German third-person pronoun -- an English filler only",
    "ah": "genuine interjection of realisation, not always a filled pause",
    "oh": "genuine interjection",
}

_TIER_A_EN = [
    "uh", "uhh", "uhm", "um", "umm", "er", "err", "erm", "mm", "mhm", "hmm",
    "hm", "huh", "eh",
]
_TIER_A_EN_AMBIGUOUS = {
    "ah": "interjection of realisation",
    "oh": "interjection",
}

for _w in _TIER_A_DE:
    _register(_w, "A", "de")
for _w, _n in _TIER_A_DE_AMBIGUOUS.items():
    _register(_w, "A", "de", ambiguous=True, note=_n)
for _w in _TIER_A_EN:
    _register(_w, "A", "en")
for _w, _n in _TIER_A_EN_AMBIGUOUS.items():
    _register(_w, "A", "en", ambiguous=True, note=_n)


# --- TIER B: lexical fillers, auto-remove behind guards ---------------------- #
# These have surface meaning but no propositional contribution in filler use.
# cut.py additionally requires a flanking gap, refuses to leave a dangling
# connector, and rate-limits tier-B removals to one per 4 s of output.

_TIER_B_DE = [
    "weißt du", "wissen sie", "sozusagen", "gewissermaßen", "im prinzip",
    "im endeffekt", "sagen wir mal", "sag ich mal", "sag mal", "und so weiter",
    "wie gesagt", "nech", "gell",
]
_TIER_B_DE_AMBIGUOUS = {
    "quasi": "can be used literally ('quasi identisch')",
    "und so": "prefix of a real coordination ('und so kam es')",
}
_TIER_B_DE_TAG = {
    "ne": "filler only as a sentence-final tag question",
    "oder": "filler only as a sentence-final tag question; a coordinator otherwise",
    "nicht wahr": "sentence-final tag question",
}

_TIER_B_EN = [
    "you know", "i mean", "sort of", "sorta", "kinda", "or something",
    "or whatever", "if you will", "at the end of the day",
]
_TIER_B_EN_AMBIGUOUS = {
    "kind of": "literal use is common ('a kind of filter')",
}
_TIER_B_EN_TAG = {
    "right": "tag question only; also a tier-C particle elsewhere",
}

for _w in _TIER_B_DE:
    _register(_w, "B", "de")
for _w, _n in _TIER_B_DE_AMBIGUOUS.items():
    _register(_w, "B", "de", ambiguous=True, note=_n)
for _w, _n in _TIER_B_DE_TAG.items():
    _register(_w, "B", "de", ambiguous=True, tag_only=True, note=_n)
for _w in _TIER_B_EN:
    _register(_w, "B", "en")
for _w, _n in _TIER_B_EN_AMBIGUOUS.items():
    _register(_w, "B", "en", ambiguous=True, note=_n)
for _w, _n in _TIER_B_EN_TAG.items():
    _register(_w, "B", "en", ambiguous=True, tag_only=True, note=_n)


# --- TIER C: modal particles and discourse markers -- FLAG ONLY -------------- #
# Never auto-removed. Present so the story pass and the human reviewer can see
# them, and so a density read of "how filler-heavy is this stretch" is honest.

_TIER_C_DE = [
    "also", "halt", "eben", "ja", "mal", "doch", "schon", "nun", "so",
    "einfach", "eigentlich", "natürlich", "genau", "praktisch", "letztendlich",
    "irgendwie", "tja", "naja", "na ja", "gut", "okay", "ok", "klar",
]
_TIER_C_EN = [
    "like", "just", "well", "right", "so", "basically", "actually", "literally",
    "obviously", "honestly", "anyway", "okay", "ok", "i guess", "you see",
]

for _w in _TIER_C_DE:
    _register(_w, "C", "de", note="modal particle: carries attitude, flag only")
for _w in _TIER_C_EN:
    _register(_w, "C", "en", note="discourse marker: flag only")


# --------------------------------------------------------------------------- #
# restart markers (used by the retake detector)
# --------------------------------------------------------------------------- #
# Lexical, so they live here with the other tables and share the normalizer.
# A marker is the strongest single interruption cue there is: marker plus a
# near-duplicate n-gram is a near-certain retake.

_RESTART_DE = [
    "nochmal", "noch mal", "nochmals", "moment", "sorry", "warte", "warte mal",
    "stopp", "stop", "schnitt", "von vorne", "quatsch", "ich mein", "ich meine",
    "neuer versuch", "take zwei", "äh nein", "nein nein", "entschuldigung",
    "falsch", "nochmal von vorne",
]
_RESTART_EN = [
    "sorry", "let me try that again", "take two", "one more time", "cut",
    "hold on", "scratch that", "start over", "let me redo that", "again",
    "my bad", "wait",
]

RESTART_MARKERS: dict[tuple[str, str], int] = {}
for _w in _RESTART_DE:
    _k = " ".join(normalize_tokens(_w.split()))
    RESTART_MARKERS[("de", _k)] = len(_k.split())
for _w in _RESTART_EN:
    _k = " ".join(normalize_tokens(_w.split()))
    RESTART_MARKERS[("en", _k)] = len(_k.split())

MAX_MARKER_TOKENS: int = max(v for v in RESTART_MARKERS.values())


# --------------------------------------------------------------------------- #
# enumeration guard vocabulary
# --------------------------------------------------------------------------- #
# "erstens ..., zweitens ..." shares its frame with the previous item and looks
# exactly like a restart to an n-gram matcher. Stripping ordinals and function
# words before the similarity test forces the match to be carried by CONTENT
# words, which is what actually distinguishes a repeat from a list.

_ORDINALS_DE = {
    "erstens", "zweitens", "drittens", "viertens", "fünftens", "sechstens",
    "erste", "zweite", "dritte", "vierte", "erster", "zweiter", "dritter",
    "zum einen", "zum anderen", "außerdem", "schließlich", "zuletzt",
}
_ORDINALS_EN = {
    "first", "firstly", "second", "secondly", "third", "thirdly", "fourth",
    "next", "finally", "lastly", "also", "moreover",
}
_FUNCTION_DE = {
    "und", "oder", "aber", "dann", "dass", "die", "der", "das", "den", "dem",
    "des", "ein", "eine", "einen", "einem", "einer", "ist", "sind", "war",
    "wir", "ich", "es", "er", "sie", "in", "im", "zu", "zur", "zum", "mit",
    "auf", "für", "von", "vom", "am", "an", "als", "bei", "nicht", "auch",
    "wie", "was", "wenn", "man", "sich", "hat", "haben", "wird", "werden",
}
_FUNCTION_EN = {
    "and", "or", "but", "then", "the", "a", "an", "is", "are", "was", "we",
    "i", "it", "in", "to", "of", "for", "with", "on", "that", "this", "at",
    "as", "not", "be", "have", "has", "will", "would",
}

STOPWORDS: dict[str, frozenset[str]] = {
    "de": frozenset(_ORDINALS_DE | _FUNCTION_DE),
    "en": frozenset(_ORDINALS_EN | _FUNCTION_EN),
}


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #

def lookup(tokens: Sequence[str], i: int, language: str = "de",
           *, tiers: Sequence[str] = TIERS) -> tuple[FillerEntry, int] | None:
    """Longest filler phrase starting at ``tokens[i]``, or None.

    ``tokens`` must already be normalized (:func:`normalize_tokens`). Returns
    ``(entry, n_tokens)``. Longest-first so ``im Prinzip`` wins over a bare
    ``im`` and ``na ja`` is one hit rather than two.
    """
    if i < 0 or i >= len(tokens) or not tokens[i]:
        return None
    upper = min(MAX_PHRASE_TOKENS, len(tokens) - i)
    for n in range(upper, 0, -1):
        window = tokens[i:i + n]
        if any(not t for t in window):
            continue
        entry = _TABLE.get((language, " ".join(window)))
        if entry is not None and entry.tier in tiers:
            return entry, n
    return None


def lookup_restart_marker(tokens: Sequence[str], i: int,
                          language: str = "de") -> tuple[str, int] | None:
    """Longest restart marker starting at ``tokens[i]``, or None."""
    if i < 0 or i >= len(tokens) or not tokens[i]:
        return None
    upper = min(MAX_MARKER_TOKENS, len(tokens) - i)
    for n in range(upper, 0, -1):
        window = tokens[i:i + n]
        if any(not t for t in window):
            continue
        phrase = " ".join(window)
        if (language, phrase) in RESTART_MARKERS:
            return phrase, n
    return None


def is_tier_a(token: str, language: str = "de") -> bool:
    """True if the single normalized token is a tier-A filled pause.

    Used by the retake detector, which drops filled pauses from the comparison
    view before matching n-grams (the pause is the interruption cue, not part of
    either attempt).
    """
    entry = _TABLE.get((language, token))
    return entry is not None and entry.tier == "A"


def is_stopword(token: str, language: str = "de") -> bool:
    return token in STOPWORDS.get(language, frozenset())


def entries(language: str | None = None, tier: str | None = None) -> list[FillerEntry]:
    """Table dump, for docs, tests and the calibration report."""
    out = [e for (lang, _p), e in _TABLE.items()
           if (language is None or lang == language) and (tier is None or e.tier == tier)]
    out.sort(key=lambda e: (e.language, e.tier, e.phrase))
    return out


def table_stats() -> dict[str, int]:
    """Counts per language/tier. QC prints this so a table edit is visible."""
    stats: dict[str, int] = {}
    for entry in _TABLE.values():
        stats[f"{entry.language}_{entry.tier}"] = stats.get(f"{entry.language}_{entry.tier}", 0) + 1
    stats["restart_markers"] = len(RESTART_MARKERS)
    return dict(sorted(stats.items()))
