# The cut algorithm

> ## <a id="calibration"></a>NOTHING HERE IS CALIBRATED
>
> **Every threshold on this page is research-derived synthesis. Not one of them
> has been checked against real footage from this speaker, this microphone, or
> this room. Nobody has yet watched a cut made with these numbers at full speed
> with sound.**
>
> The convergence argument below is real — three independent tools land in the
> same range — but convergence is not calibration. `project.json` carries
> `"calibrated": false`, every profile in `presets/cut-profiles.json` carries
> `calibrated: false` and `calibrated_against: null`, and QC check (h) warns on
> every run until that changes.
>
> Treat these as *starting points to argue with*, never as tuned values. When a
> cut sounds wrong, the number is more likely wrong than the footage.
>
> [How to calibrate](#howto) is at the bottom of this page.

---

## The shape

Four passes over the flattened word list, then a snapping pass:

```
words  ->  silence tiering  ->  filler tiering  ->  retake detection
       ->  splice hygiene   ->  integer frame snapping  ->  segments[]
```

Operate on the **flattened** word list, not per-Whisper-segment `words` arrays: a
restart that straddles a Whisper segment boundary is invisible otherwise.

When `story.blocks` exist, all four passes run **inside kept blocks only**. There
is no point detecting a retake in material that is being dropped wholesale.

Each produced segment records which pass created it in `provenance.stage`
(`content`, `silence`, `filler`, `retake`), so any decision can be traced and
reversed.

---

## Pass 1 — silence

```
gap[i] = words[i+1].start - words[i].end
```

| parameter | default | status | justification |
| --- | --- | --- | --- |
| `pad_in` | 0.08 s | UNVERIFIED | an initial /p t k b d g/ needs 20–40 ms of pre-silence for the burst to read as a consonant rather than a click |
| `pad_out` | 0.10 s | UNVERIFIED | /t k s/ releases run 60–120 ms **past** WhisperX's `end` |
| `keep_pause` floor | 0.18 s | UNVERIFIED | the breath slot left where dead air was |
| `min_gap` | 0.55 s | UNVERIFIED | below this, never cut |
| `min_removal` | 0.20 s | UNVERIFIED | a shorter removal is not worth a visible cut |
| `max_cut_density` | 1 per 1.5 s | UNVERIFIED | jump-cut fatigue |
| `min_clip` | 0.35 s | UNVERIFIED | a ~10-frame kept clip is a visual glitch on a talking head |

**The arithmetic is forced.** `pad_out + pad_in + keep_pause = 0.36 s`, so
`min_gap` must exceed `0.36 + min_removal = 0.56 s`. That is where 0.55–0.60 comes
from — it is not a taste judgement, it is the smallest gap from which a cut can
remove anything at all.

**Why 0.2 s would be robotic.** Spontaneous speech has inter-word gaps of
0–150 ms constantly; those are phonetic, not pauses. Clause boundaries sit at
200–500 ms, sentence boundaries at 500–1000 ms, topic changes at 1–2 s. A 200 ms
threshold strips every clause boundary — exactly the prosody that tells a listener
where the syntax breaks.

**The convergence argument.** auto-editor ships margin 0.2 s each side and
`mincut` 6 frames (0.2 s at 30 fps), which works out to "gaps ≤ 0.4 s untouched, a
cut is only worth making above ~0.6 s, keep ~0.4 s". Descript's guidance is
"shorten gaps > 0.5–0.7 s down to 0.2–0.25 s". Three independent tools land on
**cut above ~0.6 s, keep ~0.2–0.4 s**. (VERIFIED as a reading of auto-editor's
source; UNVERIFIED as a claim about *this* footage.)

### Tiered retained pause

A single threshold makes a clause boundary and a topic change sound identical.
So the retained pause is tiered, then scaled by the punctuation of the **preceding**
word (Whisper emits punctuation):

| original gap | base retained pause |
| --- | --- |
| < 0.55 s | untouched |
| 0.55 – 1.20 s | 0.30 s |
| 1.20 – 3.00 s | 0.45 s — reads as a deliberate beat |
| > 3.00 s | 0.60 s **and flag for review** — usually a retake boundary or notes-checking |

| preceding word ends with | multiplier | floor |
| --- | --- | --- |
| `.` `?` `!` | × 1.5 | 0.40 s |
| `,` `;` `:` | × 1.0 | 0.20 s |
| nothing (mid-clause) | × 0.7 | 0.20 s |
| topic change (from `story`) | × 2.0 | 0.60 s |

All UNVERIFIED.

### Post-passes, in order

1. **min_removal filter** — drop any cut removing < 0.20 s.
2. **density merge** — if two cuts land within 1.5 s on the *output* timeline,
   keep the larger removal and drop the other.
3. **min_clip filter** — any surviving kept segment shorter than 0.35 s merges
   into a neighbour.

---

## Pass 2 — fillers

**Three tiers. Only tier A is unconditionally automatic. Tier C is never
automatic, in any profile, ever.**

### Tier A — non-lexical, auto-remove

No propositional content, so removal is safe by construction.

- German: `äh ähh ähm ähmm äm ehm eh öh öhm hm hmm hmmm mh mhm mmh mmm äöh`
- English: `uh uhh um umm ummm er err erm ah ahh mm mhm hmm huh`

`am`, `eh`, `ah` and `so` collide with real words — they must pass the duration
test before being trusted.

### Tier B — lexical fillers, auto-remove under guards

- German: `weißt du, wissen Sie, sozusagen, quasi, gewissermaßen, im Prinzip,
  im Endeffekt, sagen wir mal, sag ich mal, und so, wie gesagt, ne?, nech, gell,
  oder?` — the tag-question forms only, i.e. sentence-final with a following gap.
- English: `you know, I mean, sort of, kind of, or something, if you will,
  at the end of the day`

Guards, all must hold: flanked by a gap ≥ 60 ms on at least one side; the removal
leaves no dangling connector; at most one tier-B removal per 4 s of output.

### Tier C — flag only, never remove

- German modal particles: `also halt eben ja mal doch schon nun so einfach
  eigentlich natürlich genau praktisch letztendlich irgendwie tja naja gut okay`
- English: `like just well right so basically actually literally obviously
  honestly anyway okay`

These carry attitude and discourse structure. `Das ist halt so` ≠ `Das ist so`.
`Also, fangen wir an` is a genuine connector, not throat-clearing. The damage from
removing them is invisible in a transcript diff and obvious on playback, which is
why it is a correctness rule and not an aggressiveness dial. Route each instance to
the LLM with ±2 sentences of context and the question: *does removing this change
the meaning, the register, or leave it ungrammatical?*

### <a id="the-score-trap"></a>The `score` trap

**Never use WhisperX's `score` as a filler signal.** It is the mean per-character
wav2vec2 **alignment** score. A crisp, correctly transcribed `ähm` scores high. A
low score means "the audio does not match these letters", which correlates with
rare words, proper nouns and code-switching — the exact opposite of a filler.

Its one legitimate use is as a timestamp-reliability flag:

```
score < 0.35  ->  widen padding by 60 ms, and forbid any cut inside that word
```

### Detection signal order

1. **Lexical match** — 90 % of the work. Normalize: lowercase, NFC, strip
   `.,!?;:…"'»«`, collapse repeated vowels (`ähhhh` → `äh`). **Keep umlauts** —
   Whisper writes them correctly; do not transliterate.
2. **Duration per character** — `(end − start) / max(1, n_chars) ≥ 0.12 s`. A
   genuine filled pause is long for its phoneme count: `ähm` ≥ ~0.36 s. A 0.09 s
   "äh" is usually a fragment of a real word.
3. **Flanking gap** — a filled pause *is* a pause you filled: expect ≥ 0.10 s on
   at least one side.
4. **Energy** — mean RMS within 6 dB of the surrounding speech. 15 dB down means
   it was a breath that Whisper hallucinated as "Ähm"; route it to the breath
   handler instead.

Combine as `lexical AND (dur_per_char OR flank_gap)` for tier A; require all four
for tier B.

### Five hard blocks on removing a filler

1. **Sentence-initial at a topic change** — the filler *is* the turn-taking cue.
2. **Coarticulated** — gaps on *both* sides < 50 ms; excising it chops the tail of
   the previous word and the head of the next.
3. **Clause-final falling pitch** — a long filler with a falling contour *is* the
   boundary tone.
4. **Visible gesture** — removing it produces a visual jump with no audio
   motivation.
5. **Density** — if you would remove > 25 % of tokens in any 5-second window, the
   speaker was struggling. Flag the whole stretch for a retake or B-roll cover.
   Surgically shaving it reads as unnaturally dense.

---

## Pass 3 — retakes

**Step 0.** Build a token list with tier-A fillers removed from the comparison but
index-mapped back to the originals.

**Step 1 — candidate interruption points `p`.** A restart almost always has one
of: a gap ≥ 0.35 s, a filled pause, or an explicit marker.

- German markers: `nochmal, noch mal, moment, sorry, warte, stopp, schnitt,
  von vorne, quatsch, ich mein(e), neuer Versuch, Take zwei, äh nein, nein nein`
- English: `sorry, let me try that again, take two, again, one more time, cut,
  hold on, scratch that, start over, let me redo that`

**Step 2 — descending n-gram match around `p`:**

```
for n in 8..3:
    A = t[p-n : p]          # tail of the reparandum
    B = t[p : p+n]          # head of the repair
    sim = 2 * LCS(A, B) / (len(A) + len(B))
    if (n >= 4 and sim >= 0.72) or (n == 3 and sim == 1.0):
        fire with reparandum length n
take the LARGEST n that fires
```

**Step 3 — extend backwards to a clean boundary.** This is what separates
"removes the stutter" from "removes the bad take". Walk left from `p−n` to the
nearest sentence start (a previous `.` `?` `!`, a gap ≥ 0.8 s, or a speaker
change) and cut from *there* to `p`. Speakers restart the whole clause, not the
matched n-gram.

**Step 4 — gates.**

| condition | action |
| --- | --- |
| `sim ≥ 0.85` and `n ≥ 4` | auto-apply |
| `0.72 ≤ sim < 0.85`, or `n == 3` | propose, showing both attempts in full |
| a marker is present | propose regardless of similarity, with a wide window |

**Step 5 — the multi-take case** (the same 30-second block three times). N-gram
matching cannot span that. Do it at sentence level: TF-IDF cosine over content
words, find runs of consecutive sentences whose best match is a *later* run with
cosine ≥ 0.80, drop the earlier run. Guards: the two runs are within 120 s of each
other, and the later run is ≥ 90 % of the earlier run's word count — otherwise a
one-line summary can silently replace a full explanation.

All thresholds UNVERIFIED.

### Failure modes and their guards

| failure | guard |
| --- | --- |
| rhetorical anaphora — "Wir bauen. Wir bauen. Wir bauen weiter." | require an interruption cue (gap ≥ 0.35 s, filled pause, or marker) between the repeats |
| enumerations — "erstens…, zweitens…" share the frame | strip ordinals and connectives before computing similarity; require *content* words to match |
| corrected facts — "das kostet 30 Euro, äh, 40 Euro" fires correctly, "30 Euro und 40 Euro zusammen" must not | same interruption-cue guard |
| the last take is the worst — speakers degrade | never delete: `enabled: false`, `reason: "retake_superseded"`, `alternatives: [seg_ids]` |
| Whisper merged the repeats | accept the miss; text-only detection cannot see it |
| speaker interleaving | with diarization on, never match a reparandum against a different speaker's repair |

Published text-only disfluency detectors top out at **F 86–91 %**. Expect roughly
one miss or false fire per ten disfluencies. That is exactly why every retake
decision is a reversible flag.

---

## Pass 4 — splice hygiene

### Why cuts click

At a splice you join sample *x* from one place to sample *y* from another. If they
differ you have created a near-instantaneous step, and a step is broadband — that
is the click. It has nothing to do with the gap being "silent": room tone is never
zero, and a step at −40 dBFS is clearly audible on headphones.

Measured (research, this machine — 220 Hz sine at 48 kHz, cut at deliberately
mismatched phase):

| condition | max sample-to-sample step |
| --- | --- |
| continuous signal (baseline) | 119 LSB |
| hard cut, mid-phase | **3508 LSB — 29.5× baseline** |
| zero-crossing snap, no fade | 119 LSB |
| crossfade 5 / 10 / 20 / 40 ms | 119 / 119 / 120 / 119 LSB |

### Do both

1. **Zero-crossing snap.** From the frame-snapped cut sample, search ±5 ms
   (±240 samples at 48 kHz) for a sample where the mono-summed waveform crosses
   zero with the same slope sign on both sides. Costs nothing, is exact, does not
   touch the signal. **Sub-frame and audio-only — it must never move the video
   cut.** Stored as `audio.zero_cross_offset_samples`.
2. **Always also crossfade.** 20 ms default, 10 ms minimum, 40 ms maximum. 20 ms
   at 48 kHz is 960 samples; its lowest fully resolved frequency is ~50 Hz, so it
   does not audibly duck speech. `curve=tri` when both sides are correlated room
   tone from the same take; `curve=qsin` across takes or mics.
3. **Never crossfade longer than half the retained pause.** Cutting *inside*
   speech (filler removal) drops to 10 ms.

> `acrossfade` defaults to `o=1` and **shortens the join by exactly `d`**. This
> pipeline does not use it. The audio bed is built from explicit `afade` +
> `adelay=<n>S` + `amix=normalize=0`, where the arithmetic is ours.

### Breath

A breath is 150–500 ms, 12–25 dB below speech, energy in 500–4000 Hz, no F0. It
lives *inside* a removed gap, so the word-level pass never sees it.

Do **not** delete every breath — a talking head with none reads as synthetic.

- **Keep** the anticipatory breath before any sentence following a pause > 0.8 s.
- **Attenuate** other breaths by −12 dB rather than deleting them.
- **Delete** only breaths longer than 400 ms or louder than −18 dBFS relative to
  speech.

### Plosive and fricative safety

- `pad_in = 0.08 s` buys the pre-silence an initial /p t k b d g/ needs.
- `pad_out = 0.10 s` covers /t k s/ releases running past WhisperX's `end`.
- **Never place a cut inside a fricative.** A chopped /s/ or /ʃ/ is very audible.
  If the snapped cut lands within 40 ms of a high-zero-crossing-rate, high-HF
  region, push it outward to the next low-energy sample.

---

## Frame snapping

Padding is applied **before** snapping. This is the exact code; do not "simplify"
it to `round()`:

```python
t_in  = word.start - pad_in
t_out = word.end   + pad_out
# clamp both to [0, source_duration]

in_frame  = math.floor(t_in  * fps + 0.5)    # round half UP   -> ties land later
out_frame = math.ceil (t_out * fps - 0.5)    # round half DOWN -> ties land earlier
if out_frame <= in_frame:
    out_frame = in_frame + 1
```

**Ties are systematic, not rare.** WhisperX rounds every timestamp to 3 decimals.
At 25 fps, frame boundaries are exact multiples of 0.04 s and 3-decimal times hit
them or their midpoints constantly; at 30 fps, 1/30 = 0.0333… and 3-decimal times
land on .0166/.0167 regularly.

**Getting OUT wrong costs 6.7 s.** VERIFIED this pass at 30 fps:
`trim=start=1.0:end=2.0` gives 30 frames, `end=2.0166667` (the frame-60.5 tie)
gives **31**. Round OUT half up and every segment is one frame long — 200 cuts is
200 extra frames ≈ 6.7 s, and because audio is computed in samples, the audio ends
6.7 s out of sync.

**Snapping happens once, on the project timebase.** Intake has already transcoded
every source to that one CFR rate, so there is no source-vs-timeline rate to
reconcile and no drop-frame arithmetic anywhere.

**Audio at NTSC rates cannot be frame-exact and does not need to be.** At 48 kHz /
29.97, one frame is 48000 × 1001 / 30000 = 1601.6 samples. Video snaps to frames;
audio snaps to samples derived from the frame-snapped times; they agree to within
0.6 samples ≈ 12 µs, which is inaudible and does not accumulate.

**A relative note is a delta on the integer.** "Move that cut 200 ms earlier" at
29.97 is `0.200 × 29.97 = 5.994 → 6 frames`, applied to `src_out_frames`. Round the
delta the same way a timestamp is rounded. Worked end to end in
[edl-schema.md](edl-schema.md#worked-example).

---

## J and L cuts

Letting the incoming audio arrive 4–12 frames before the picture is the single
cheapest thing that makes a jump cut read as *edited* rather than *machine-severed*.
Default 6 frames. Beyond ~20 frames it reads as a mistake.

**But never on a same-source jump cut.** This is a validator rule, not a
guideline:

> Reject `lead_frames != 0 || tail_frames != 0` on any join where
> `segments[k].source_id == segments[k+1].source_id` **and** neither side is
> covered by B-roll or a full-frame overlay.

On a visible mouth, sliding the audio is lip-sync error. Legitimate uses: angle
changes, B-roll, screen recordings, and joins where a graphic covers the picture.

Additional constraints:

- `lead_frames + tail_frames ≤ min(len(A), len(B)) − 1` across any single join
- `audio_src_in ≥ 0` and `audio_src_out ≤ source_duration_frames`
- a J-cut must not pull audio from across a **removed** region — that would
  resurrect the dead air just cut. Clamp `lead_frames` to stay inside previously
  kept material.

J/L cuts force audio and video onto separate render passes. The AAC-drift finding
forces the same thing independently. Two constraints agreeing is a good sign the
architecture is right.

---

## Profiles

`presets/cut-profiles.json` ships three: `conservative`, `balanced`, `aggressive`.
`balanced` **is** this page's defaults verbatim. `conservative` widens the pads
~50 %, raises `min_gap` to 0.85 s, and demotes tier B to flag-only. `aggressive`
tightens for short form.

Tier C stays flag-only in all three. Being in a hurry is not a reason to change
what a sentence means.

---

## <a id="howto"></a>How to calibrate

1. **Record a fixture.** 3–5 minutes of real German talking head into
   `assets/samples/`, deliberately containing: one full retake of a sentence, one
   long `ähm`, one hard topic change, one compound noun over 20 characters, one
   sentence ending in a plosive, and one passage of genuine thinking-pause that
   must **not** be cut.
2. **Transcribe once** and freeze the `words.json` as a fixture so re-runs are
   comparable.
3. **Hand-mark ground truth**: for every gap — cut or keep, and if kept, how long?
   For every filler token — remove or keep? For the retake — where does the
   reparandum start?
4. **Score each profile** against it: false cuts (removed something meaningful),
   missed cuts (dead air survived), audible splices (clicks, clipped plosives,
   breaths cut mid-inhale).
5. **Move one parameter at a time.** Start with `pad_out` — it is the most audible
   one, because clipping a /t k s/ release is what makes a cut sound spliced.
6. **Watch the result at full speed with sound.** Frame-accurate is not the same
   as listenable. Jump-cut fatigue only appears in playback, never in a diff.
7. When a profile survives, set `calibrated: true` and `calibrated_against` to the
   fixture's `strong_key`, so changing the fixture invalidates the claim instead
   of silently keeping it. Then set `"calibrated": true` in `project.json` and
   QC check (h) stops warning.
