# The pipeline, stage by stage

Every stage reads `project.json`, does one thing, writes `project.json` back, and
advances the `stage` field. Nothing else is state. If a stage crashes halfway, the
next run starts from the same document and the content-addressed cache means the
work that already succeeded is not repeated.

`stage` takes exactly these values, in this order:

```
intake -> transcribed -> story -> rough_cut -> graphics -> captions -> music -> export
```

`stage` records what has **completed**, not what is running. A project sitting at
`rough_cut` has a finished rough cut and has not yet had graphics applied.

---

## 1. Intake — `stage: intake`

**Consumes** raw camera/screen files anywhere on disk.
**Produces** `projects/<slug>/media/*.mp4` (CFR, project timebase) and one entry
per file in `sources[]`.

Intake is not optional and it is not a copy. Every source is **transcoded** to the
project's constant integer frame rate and to 48 kHz audio. This is the single
decision that removes the entire class of bugs around variable frame rate,
drop-frame timecode, and mixed-rate timelines: after intake there is exactly one
timebase in the project and `src_in_frames` means the same thing for every source.

VFR sources make this mandatory rather than merely advisable — ffprobe and other
tools disagree about how many frames a VFR mp4 even contains (113 vs 111 on the
same file, measured during research), so a VFR file has no stable frame index to
snap to. Phone recordings, OBS captures and screen recorders are routinely VFR.

What intake records per source:

| field | why |
| --- | --- |
| `path` | project-relative, points at the **normalized** file |
| `original_path` | absolute, so the master is always traceable; never written to |
| `strong_key` | `sha256` of the whole normalized file — the root of every cache key |
| `probe` | frame count, rate, sample rate, channels, codecs, dimensions |
| `role` | `acam` \| `broll` \| `screen` \| `music` — drives cut and J/L-cut rules |

`duration_frames` in `probe` comes from `ffprobe -count_frames`, which decodes the
whole file. `nb_frames` is absent in MKV and wrong often enough elsewhere that it
is never trusted (VERIFIED, research track 5).

Originals are never modified, never moved, never deleted.

---

## 2. Transcribe — `stage: transcribed`

**Consumes** the `acam` source's audio.
**Produces** `.cache/transcript/<strong_key>.words.json` and the `transcript` block.

The main pipeline does not import WhisperX — it cannot, the system Python is 3.14
and WhisperX caps at `<3.14`. Transcription runs in `.venv-asr` (Python 3.12) and
is reached only through `subprocess`. See [setup.md](setup.md) for the three
tiers and the cuDNN PATH fix.

The normalized word record is what the rest of the pipeline sees:

```json
{"word": "Frame", "start": 12.184, "end": 12.462, "score": 0.91,
 "seg_idx": 4, "speaker": null, "interpolated": false}
```

`interpolated: true` marks a word whose timestamp wav2vec2 could not measure and
that was filled in from its neighbours. Cutting inside an interpolated word is
forbidden. `score` is an **alignment** score and must never be used as a filler
signal — see [cutting.md](cutting.md#the-score-trap).

The `transcript` block records `engine`, `align_model`, `language` and
`timestamp_source` (`wav2vec2-forced-align` | `dtw` | `ffmpeg-whisper-token`), so
a later reader can tell how trustworthy the timestamps are without re-running
anything.

---

## 3. Story — `stage: story`

**Consumes** the flattened transcript text.
**Produces** `story.blocks[]` and `story.hook`.

This is the content pass and it is the reason the system produces a *video*
rather than a tightened recording. Claude reads the transcript, decides what the
piece is about, which topic blocks survive, what order they go in, and where the
hook is. Mechanical cutting then runs **inside the kept blocks only**, which is
both better editing and cheaper — no point running retake detection over material
that is being dropped wholesale.

```json
{"id": "blk_03", "word_range": [812, 1104],
 "label": "Warum Frame-Snapping zählt",
 "decision": "keep", "order": 2, "rationale": "kernargument, trägt das video"}
```

`decision` is `keep` | `drop` | `maybe`. `order` may differ from recording order —
that is how a hook gets pulled to the front. `maybe` blocks are surfaced to the
human; nothing is silently discarded.

---

## 4. Rough cut — `stage: rough_cut`

**Consumes** words + story blocks + a cut profile from `presets/cut-profiles.json`.
**Produces** `segments[]`.

Four passes: silence tiering, filler tiering, retake detection, splice hygiene —
then integer frame snapping. The full algorithm, every threshold and its
justification, is in [cutting.md](cutting.md). **None of those thresholds have
been calibrated against real footage yet.**

Each segment carries `provenance.stage` naming which pass created it (`content`,
`silence`, `filler`, `retake`), so a human can flip one boolean at either level
and see why the machine did what it did.

Rejected material becomes `"enabled": false` with a `reason`. It stays in the
file forever.

---

## 5. Graphics — `stage: graphics`

**Command** `.\tools\ve.ps1 gfx <slug>` (add `--workers N`; 4 by default).
**Consumes** `presets/graphics/*.html` + `overlays[].props`.
**Produces** one alpha movie per overlay in `.cache/ovl/`, writes the
`{cache_key, path, frames}` record back into `overlays[].render`, and runs QC
checks (d) and (g) over what it just made.

Compositing does **not** happen here. `ve render` overlays the movies onto the
finished master at the position `_computed.positions` derives, then burns the
captions on top of that. So the running order is `gfx` -> `captions` -> `render`,
and re-running `gfx` or `captions` afterwards leaves the master stale.

Headless Chrome renders each frame as a pure function of `?t=<seconds>`; the PNG
sequence is encoded to `qtrle` `argb`. Measured on this machine: **398 ms/frame**
at 1920x1080, byte-identical output for the same URL. Full mechanics and a
complete annotated template are in [graphics.md](graphics.md).

Overlays anchor to `{segment_id, offset_frames}`, never to an absolute timeline
position, so an upstream re-cut moves them automatically.

---

## 6. Captions — `stage: captions`

**Consumes** words + the caption preset.
**Produces** `out/captions.ass`, optionally burned in at render.

Word grouping uses a **pixel-width budget**, not a character cap. German compound
nouns are single unsplittable tokens: `Geschwindigkeitsbegrenzung` at 90 px
measures **919 px in Montserrat ExtraBold — 48 % of a 1920 frame**, and
**1112 px in Inter ExtraBold — 58 %** (VERIFIED this pass, two ways: a libass
render measured 919 px and `captions.estimate_text_width_px()` predicts 924 px
from the TTF's own `hmtx`/`cmap`). Any `max_chars` rule either breaks mid-word or
overflows the frame. Details in [ffmpeg-cookbook.md](ffmpeg-cookbook.md#captions).

---

## 7. Music and loudness — `stage: music`

**Consumes** `segments[]` + `audio_tracks[]`.
**Produces** one `pcm_s24le` master of the whole timeline.

The entire audio timeline is rendered in **one** ffmpeg pass:

```
atrim -> asetpts -> afade -> adelay=<n>S -> amix=normalize=0 -> pcm_s24le
```

There is no drift because there are no joins. Verified this pass: a two-segment
graph (1.000 s + 0.500 s) produced **72000 / 72000 samples, duration exactly
1.500000 s**. The same two segments rendered separately and joined with the
concat demuxer came out **520 samples (10.8 ms) short over a single junction** —
which is exactly why audio is never cached per segment.

Then two-pass `loudnorm` to the platform target from `presets/platforms.json`.
Pass 2 must carry `-ar 48000` or the master silently becomes 192 kHz (VERIFIED
this pass).

---

## 8. Export — `stage: export`

**Consumes** the segment cache + the audio master.
**Produces** `out/final.mp4`.

1. `concat_compatible()` on every cached segment. The concat demuxer does **not**
   validate its inputs: joining 1280x720 with 640x360 exits 0 and advertises
   1280x720 for the whole file (VERIFIED this pass). This guard is the only thing
   between a parameter mismatch and a silently corrupt master.
2. `-f concat -safe 0 -c copy` to join the video. Lossless and near-instant.
3. Mux the joined video against the audio master, encoding AAC exactly once.
4. `run_qc()` — see [the QC contract](#qc).

---

## Why video is cached but audio is not

| | video | audio |
| --- | --- | --- |
| unit | one file per segment, content-addressed | one file for the whole timeline |
| joined by | `concat -c copy` | nothing — there are no joins |
| re-render on an edit | exactly the segments whose key changed | the whole bed (seconds) |
| drift at a junction | 0 frames (VERIFIED: 30+15 = 45) | n/a |

Per-segment audio caching was measured to lose 520 samples per junction even with
PCM, and AAC segments drift ~10.7 ms per junction. Separately, J/L cuts,
per-splice crossfades and sub-frame zero-crossing nudges cannot be expressed in a
per-segment audio cache at all. Both constraints point the same way.

---

## <a id="qc"></a>The QC contract

`run_qc(project, checks=None) -> {"ok": bool, "findings": [...]}`. Each finding is
`{severity, code, stage, message, evidence_path}`. The CLI exits **0** clean,
**1** warnings, **2** failures.

| # | code | what it proves |
| --- | --- | --- |
| a | `frame_count` | output frames == Σ `(out − in)` over enabled segments |
| b | `audio_samples` | sample count == `frames / fps × 48000` |
| c | `concat_params` | every segment matches, checked **before** `-c copy` |
| d | `alpha` | one raw rgba pixel of a graphic reads `a=0` |
| e | `font_fallback` | a caption render hashes differently from an Arial control |
| f | `loudness` | `ebur128` on the master vs the platform target |
| g | `chrome_blank` | a graphic frame is not a uniform blank page |
| h | `calibration` | warns while `project.calibrated` is false |

Every check that can produce a lookable artifact writes one to `evidence_path`. A
frame-count mismatch is a number; a frame-count mismatch plus a filmstrip is a
diagnosis.

Check (h) is a warning and never a failure, but it is not decorative: while it
fires, the cut thresholds in this repo are research defaults that nobody has
watched at full speed. See [cutting.md](cutting.md#calibration).
