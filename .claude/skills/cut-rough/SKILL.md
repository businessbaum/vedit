---
name: cut-rough
description: This skill should be used when the user asks for mechanical tightening of material that is already selected - "mach den Rohschnitt", "bau den Rohschnitt", "schneid die Ähms raus", "nimm die Füllwörter raus", "entferne die Pausen", "kürz die Pausen", "schneid die Versprecher raus", "mach den Radioschnitt", "straff das", "build the rough cut", "cut the fillers", "remove the ums", "tighten the pauses", "trim the dead air", "cut the retakes", "radio edit". Turns kept story blocks into enabled segments in project.json. Does not decide what the video is about - that is cut-story.
argument-hint: [project-slug]
arguments: [project]
---

# cut-rough

Mechanical tightening: dead air, filler words, retakes, splice hygiene. Everything here is
reversible and every decision carries provenance.

Argument `$project` is the slug; full request in `$ARGUMENTS`. An empty `$`-placeholder
means the skill auto-triggered rather than being slash-invoked: take the project from
`PIPELINE_STATE` in the session context and say which one you picked. Verb names follow the
build contract; `python engine/ve.py --help` wins on conflict.

## Preconditions, and why they are strict

Run cut-story first. If `story.blocks` is missing, say so and offer cut-story rather than
proceeding - mechanical cutting over material that is about to be dropped wholesale burns
time on retake and filler detection nobody will hear.

If the user explicitly wants a mechanical pass over everything ("einfach nur die Ähms raus"),
that is legitimate. Do it, and say that no content selection happened.

## The frame rule

Cut points snap to whole frames. This is not tidiness - cut points off the frame grid make
the concat filter accumulate 8.33 ms of A/V drift per cut, 1250 ms over 150 cuts. Snapped,
the same 150 cuts drift by exactly 0 samples.

```
in_frame  = floor(t_in  * fps + 0.5)    # round half UP   -> ties land later
out_frame = ceil (t_out * fps - 0.5)    # round half DOWN -> ties land earlier
if out_frame <= in_frame: out_frame = in_frame + 1
```

`out` is **exclusive** - ffmpeg implements `trim=end` as a strict `<`, and half-open
`[in, out)` is what makes concatenation exact. Get the OUT rounding wrong and every segment
becomes one frame long; 200 cuts is 6.7 s of drift and audibly broken sync.

Never do this arithmetic by hand. `vedit/timebase.py` owns it: `snap_in` / `snap_out`.

## Step 1 - run the passes

```
python engine/ve.py cut $project
```

Four passes over the flattened word list, then the snap:

1. **Silence tiering.** Gaps below `min_gap` are never cut. Above it, the retained pause
   scales with the punctuation of the preceding word: `.?!` x1.5 (floor 0.40 s), `,;:` x1.0,
   mid-clause x0.7 (floor 0.20 s), topic change x2.0 (floor 0.60 s). A uniform pause length
   is the single most recognisable sign of a machine edit - the ear expects a longer breath
   after a full stop.
2. **Filler tiering.** See below.
3. **Retake detection.** Repeated n-grams where the later take supersedes the earlier one.
4. **Splice hygiene.** Padding, crossfades, zero-crossing snap, cut-density limiting.

## Step 2 - filler tiers, and the one that must not be automated

- **Tier A - non-lexical** (`äh ähm hm mh uh um er`): auto-remove. Nobody wants these.
- **Tier B - lexical fillers** (`weißt du, sozusagen, quasi, im Prinzip, you know, I mean`):
  auto-remove with guards - not at a sentence start, not when removal leaves a sub-`min_clip`
  fragment.
- **Tier C - modal particles**: `also halt eben ja mal doch schon eigentlich genau`, and
  English `like just well right so basically actually`. **Flag only. Never auto-remove.**

Tier C is where automated editors ruin German speech. `doch`, `eben`, `halt` and `ja` carry
attitude and discourse structure; stripping them does not make the speaker sound tighter, it
makes them sound like a translation. If the user asks to remove them anyway, do it - but say
once what it costs, then stop mentioning it.

Detection signal order: lexical match, then `duration/char >= 0.12 s`, then flanking gap
>= 0.10 s, then energy within 6 dB of surrounding speech.

**Never use WhisperX `score` as a filler signal.** It measures alignment, not hesitation. Low
scores cluster on rare words and proper nouns - the opposite of fillers. Use it only as a
timestamp-reliability flag: `score < 0.35` widens padding by 60 ms and forbids a cut inside
that word.

## Step 3 - the defaults, and their status

| parameter | default | note |
| --- | --- | --- |
| `pad_in` | 0.08 s | plosive closure before /p t k b d g/ |
| `pad_out` | 0.10 s | /t k s/ releases run past the aligner's `end` |
| `keep_pause` | 0.18 s floor | the breath slot left where dead air was |
| `min_gap` | 0.55 s | below this, never cut |
| `min_removal` | 0.20 s | shorter removals are not worth a splice |
| `max_cut_density` | 1 per 1.5 s | jump-cut fatigue |
| `min_clip` | 0.35 s | shorter kept clips merge into a neighbour |
| crossfade | 20 ms (10 min, 40 max) | `curve=tri` same take, `qsin` across takes |
| zero-cross search | +/-5 ms (240 samples) | audio only, never moves video |

**These are research-derived, not perceptually tested.** While `project.json` has
`"calibrated": false`, say so whenever you defend a cut. Do not present an uncalibrated
threshold as a tuned one. The session hook surfaces this flag every session for that reason.

The zero-crossing snap is worth understanding: a hard cut at a non-zero-crossing produced a
sample step 29.5x larger than the signal's natural maximum - an audible click. Both a
zero-crossing snap and a 20 ms crossfade bring it back to baseline. Use both. The snap moves
audio only, never video; a sub-frame video shift is not representable.

## Step 4 - J/L cuts

4-12 frames is the useful range; default 6.

**Reject any J/L cut where both sides share a `source_id` and neither is covered by B-roll or
a full-frame overlay.** On a visible mouth that is not a J-cut, it is lip-sync error, and it
reads as a technical fault rather than as editing.

## Step 5 - write segments

Segments are written into `segments[]` with `src_in_frames` (inclusive) and `src_out_frames`
(exclusive) as the only authority. Also required on every segment:

- `enabled` - **rejected material is `false` with a reason, never removed.** Every automatic
  decision must be reversible by flipping one boolean. This is the property that makes an
  aggressive default safe.
- `order` - explicit output order, dense over enabled segments.
- `provenance` - `stage` (`content` for story-level drops, `silence`, `filler`, `retake`),
  `rule`, `confidence`, `applied_by`, and the measured value that triggered it
  (`original_gap_s`). Provenance is what lets the user say "undo all tier-B removals".
- `word_range` and denormalized `text`. **The text is for reading only and is never
  authoritative** - if it disagrees with `word_range`, the word range wins.

Timeline positions are **derived**, never stored. They are a running sum over enabled
segments written into `_computed` on load. Never hand-write `_computed`.

Set `stage` to `"rough_cut"`.

## Step 6 - report like an editor, not like a script

Report: source duration, cut duration, percentage removed, cut count, cut density
(cuts per minute), and the count per provenance rule. Then flag anything that reads as an
over-cut: a stretch with more than one cut per 1.5 s, any kept clip near `min_clip`, and the
number of tier-C fillers flagged but left alone.

Offer three next steps: preview a section, adjust a threshold, or move to graphics/captions.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every segment is 1 frame | OUT rounded up instead of down | Use `snap_out`; never inline the math |
| Audible clicks at splices | Zero-crossing snap skipped | Snap plus a 20 ms crossfade |
| Speech sounds chopped and breathless | Uniform retained pause | Apply the punctuation-scaled table |
| Speaker sounds robotic and un-German | Tier C removed | Tier C is flag-only; restore by provenance |
| Cuts land mid-word | Transcript is tier 3 (BPE tokens) | Re-transcribe; token timestamps cannot be cut on |
| A/V drift grows across the video | Cut points not frame-snapped | Snap every cut point, then re-run QC check (a) |
