# `project.json` — schema v1.1

One file per project at `projects/<slug>/project.json`. It is the **only**
authority. Renders derive from it; nothing derives it from a render.

Five rules hold everywhere in this document:

1. `src_in_frames` / `src_out_frames` are **integers on the project timebase** and
   are the only authority. `in` is inclusive, `out` is **exclusive**.
2. **Timeline positions are derived.** `_computed` is rewritten on every load.
   Nothing hand-edits it and nothing reads it as authority.
3. **Nothing is ever deleted.** Rejected material is `"enabled": false` plus a
   `reason` and `provenance`.
4. **Overlays anchor to `{segment_id, offset_frames}`**; captions anchor to word
   indices. An absolute position would break on every upstream re-cut.
5. **Paths are project-relative.** The loader resolves them against the project
   directory. The cache is global and keyed purely on content, so two projects
   sharing a B-roll clip share its renders.

---

## The document

```jsonc
{
  "schema_version": "1.1",
  "project_id": "ep042",
  "created_utc": "2026-08-20T21:00:00Z",
  "stage": "rough_cut",          // intake|transcribed|story|rough_cut|graphics|captions|music|export
  "format_preset": "longform",

  "output": {
    "platform": "youtube",       // youtube|tiktok|instagram|linkedin -> loudness + safe area
    "path": "out/final.mp4",
    "resolution": [1920, 1080],
    "v_codec": "h264_nvenc", "cq": 20, "preset": "p6",
    "a_codec": "aac", "a_bitrate": "320k",
    "loudnorm": { "I": -14, "TP": -1.5, "LRA": 11 }
  },

  "timebase": { "fps_num": 30, "fps_den": 1, "sample_rate": 48000 },
  "calibrated": false,           // true only after thresholds were checked against a real fixture

  "sources": [{
    "id": "A",
    "path": "media/cam_a.mp4",   // ALREADY CFR-normalized to the project timebase
    "original_path": "C:/Users/me/Videos/raw/cam_a.MP4",
    "role": "acam",              // acam|broll|screen|music
    "strong_key": "sha256:9f2c...",
    "probe": { "duration_frames": 55040, "fps_num": 30, "fps_den": 1,
               "sample_rate": 48000, "channels": 2,
               "v_codec": "h264", "a_codec": "pcm_s16le",
               "width": 1920, "height": 1080 }
  }],

  "transcript": {
    "source_id": "A",
    "path": "../../.cache/transcript/<strong_key>.words.json",
    "engine": "whisperx-3.8.6",
    "align_model": "VOXPOPULI_ASR_BASE_10K_DE",
    "language": "de",
    "timestamp_source": "wav2vec2-forced-align",   // or "dtw" or "ffmpeg-whisper-token"
    "word_count": 4821
  },

  "story": {                     // the CONTENT-selection pass, before mechanical cutting
    "blocks": [{
      "id": "blk_03", "word_range": [812, 1104], "label": "Warum Frame-Snapping zählt",
      "decision": "keep",        // keep|drop|maybe
      "order": 2,                // output ordering; may differ from recording order
      "rationale": "kernargument, trägt das video"
    }],
    "hook": { "word_range": [2201, 2260], "note": "als kaltöffner nach vorne gezogen" }
  },

  "segments": [{
    "id": "seg_0012",
    "source_id": "A",
    "src_in_frames": 5400,       // AUTHORITY. integer. INCLUSIVE.
    "src_out_frames": 5610,      // AUTHORITY. integer. EXCLUSIVE.
    "enabled": true,
    "order": 12,                 // explicit output order
    "speed": 1.0,
    "word_range": [812, 848],
    "text": "denormalized for reading only — NEVER authoritative",
    "audio": {
      "src_in_frames": null,     // null = mirror video
      "src_out_frames": null,
      "lead_frames": 0,          // J-cut: audio arrives N frames before picture
      "tail_frames": 0,          // L-cut: audio runs N frames past picture
      "fade_in_ms": 20, "fade_out_ms": 20,
      "zero_cross_offset_samples": 0,   // sub-frame, audio only, never moves video
      "gain_db": 0.0
    },
    "locked": false,
    "provenance": { "stage": "silence", "rule": "gap_tier_2", "confidence": 0.97,
                    "applied_by": "auto", "original_gap_s": 1.42 },
    "render": { "cache_key": "b3f7c1...", "frames": 210 }
  }],

  "overlays": [{
    "id": "ovl_1", "kind": "html", "template": "presets/graphics/lower_third.html",
    "props": { "title": "Frame-Snapping", "subtitle": "warum Ganzzahlen gewinnen" },
    "anchor": { "segment_id": "seg_0003", "offset_frames": 12 },   // NEVER absolute
    "duration_frames": 180, "layer": 10,
    "render": { "cache_key": "7d21ab...", "path": "../../.cache/ovl/7d/7d21ab....mov" }
  }],

  "captions": {
    "enabled": false, "burn_in": true,
    "preset": "shortform-highlight",
    "sidecar_path": "out/captions.ass",
    "overrides": [{ "word_index": 1204, "text": "GmbH" }]
  },

  "audio_tracks": [
    { "id": "vo", "kind": "dialogue", "source": "segments", "gain_db": 0.0 },
    { "id": "music", "kind": "music", "path": "../../assets/music/bed_01.wav",
      "timeline_in_frames": 0, "gain_db": -22,
      "fade_in_s": 1.5, "fade_out_s": 3.0,
      "ducking": { "enabled": true, "sidechain": "vo",
                   "threshold": 0.03, "ratio": 8, "attack_ms": 20, "release_ms": 400 } }
  ],

  "_computed": {
    "note": "DERIVED — do not hand-edit. Rewritten on every load.",
    "generated_utc": "...", "total_frames": 26430, "total_seconds": 881.0,
    "total_samples": 1409600, "enabled_segment_count": 178,
    "positions": { "seg_0012": { "tl_in": 4102, "tl_out": 4312 } }
  }
}
```

---

## Field notes that are not obvious

**`out` is exclusive.** ffmpeg implements `trim=end` as a strict `<`. Half-open
`[in, out)` is what makes concatenation exact: `out_frame` of segment *k* is the
frame you did *not* include, so lengths sum with no off-by-one and no duplicated
or dropped frame at any join. Verified on this machine this pass, at 30 fps:

| `trim=start=1.0:end=…` | frames |
| --- | --- |
| `2.0` | **30** |
| `2.0166667` (exactly the frame-60.5 tie) | **31** |
| `2.0333333` (frame 61) | **31** |

That is why OUT rounds half **down** while IN rounds half **up**. Get it backwards
and every segment is one frame long; over 200 cuts that is ~6.7 s of length error
and audio that ends visibly out of sync.

**`text` is denormalized.** It exists so a human or Claude can read the document.
It is never parsed and never authoritative. If it disagrees with `word_range`,
`word_range` wins.

**`speed`** is a float, and it is the one place a float touches a cache key.
`keys.canonical_json()` collapses an integral float to an int for exactly this
reason: `1.0` read back from JSON and `1` written by Python must hash the same, or
every round-trip through `project.json` is a silent 100 % cache miss.

**`locked: true`** means an automatic pass may not modify this segment. It does not
mean the human cannot.

**`audio.zero_cross_offset_samples`** is sub-frame and audio-only. It never moves
the video edit point. Legal range is ±240 samples (±5 ms at 48 kHz).

**J/L cuts** (`lead_frames` / `tail_frames`) are rejected on any join where both
sides share a `source_id` and neither is covered by B-roll or a full-frame
overlay. On a visible mouth that is not a technique, it is lip-sync error.

---

## The cache-key rule

A segment's cache key is `sha256(canonical_json(payload))[:32]` over **only** what
changes the pixels:

```jsonc
{
  "v": 3,                                 // cache-schema version; bump to invalidate everything
  "source_key": "sha256:9f2c…",           // strong key of the source FILE
  "src_in_frames": 5400,
  "src_out_frames": 5610,
  "timebase": [30, 1],
  "out_resolution": [1920, 1080],
  "pix_fmt": "yuv420p",
  "speed": 1,
  "filtergraph": "trim=…,setpts=PTS-STARTPTS,scale=1920:1080:flags=lanczos",
  "encoder": { "c": "h264_nvenc", "preset": "p6", "cq": 20, "g": 30 },
  "tool": { "ffmpeg": "8.1.1-full_build-www.gyan.dev", "nvidia_driver": "610.74" }
}
```

**What must never enter the key** — putting any of these in destroys the entire
cache on every trivial edit:

- timeline position, segment id, segment index
- anything about a neighbouring segment
- captions, music, colour grade, overlays composited in a later pass
- `enabled`, `reason`, `provenance`, `locked`, `text`

Every artifact gets a `<key>.json` sidecar holding the key inputs verbatim. When
something re-renders that should not have, you diff two sidecars and see exactly
which field moved. Without it you are guessing.

Add the NVIDIA driver version to the key whenever the encoder is NVENC: NVENC is
byte-reproducible run-to-run but is not contractually stable across driver
updates.

---

## <a id="worked-example"></a>Worked example — "move that cut 200 ms earlier"

This is the payoff of the whole design, and the numbers below are **real output**
from `vedit.Project` on this machine, not an illustration.

A 29.97 project (`fps_num: 30000, fps_den: 1001`) with three segments:

```
seg_0011  src [5100, 5400)   300 frames
seg_0012  src [5400, 5610)   210 frames
seg_0013  src [5900, 6100)   200 frames
```

Loaded, `_computed` says:

```
total_frames 710   total_seconds 23.690333   total_samples 1137136
positions {"seg_0011": {"tl_in": 0,   "tl_out": 300},
           "seg_0012": {"tl_in": 300, "tl_out": 510},
           "seg_0013": {"tl_in": 510, "tl_out": 710}}
```

**Step 1 — convert the note to frames.** Round the *delta* exactly the way a
timestamp is rounded:

```python
tb.seconds_to_frames_delta(0.200)   # 0.200 * 29.97 = 5.994  ->  6
```

**Step 2 — change one integer.**

```python
project.segment("seg_0012")["src_out_frames"] -= 6     # 5610 -> 5604
project.save()
```

**Step 3 — there is no step 3.** `save()` recomputes and the document now reads:

```
total_frames 704   total_seconds 23.490133   total_samples 1127526
positions {"seg_0011": {"tl_in": 0,   "tl_out": 300},
           "seg_0012": {"tl_in": 300, "tl_out": 504},
           "seg_0013": {"tl_in": 504, "tl_out": 704}}
```

23.690333 − 23.490133 = **0.200200 s**, which is exactly 6 frames at 30000/1001.
The audio moved with it: 1137136 − 1127526 = 9610 samples = 0.200208 s.

What re-renders: `seg_0012`'s cache key changed, so **one** segment out of three.
`seg_0011` and `seg_0013` keep their keys and their cached files untouched — their
timeline positions moved, and timeline position is deliberately not in the key.
Then one audio pass and one stream-copy concat. Minutes become seconds.

Overlays anchored to `seg_0012` follow automatically, because they store
`offset_frames` from the segment, not an absolute frame.

**If the note meant "start the incoming shot 200 ms sooner" instead**, it is the
same shape on the other side of the cut:

```python
project.segment("seg_0013")["src_in_frames"] -= 6      # 5900 -> 5894
```

Also one integer, also one segment re-rendered. Ask which one is meant when it is
ambiguous — the two edits look identical in a description and produce different
videos.

---

## Reversing an automatic decision

Nothing is deleted, so undoing an automatic cut is one boolean:

```jsonc
{
  "id": "seg_0019", "enabled": false,
  "reason": "retake_superseded",
  "alternatives": ["seg_0024"],
  "provenance": { "stage": "retake", "rule": "ngram_restart", "n": 6,
                  "similarity": 0.91, "confidence": 0.88, "applied_by": "auto" }
}
```

Set `enabled: true` and the segment comes back with its original in/out. Set the
superseding segment's `enabled: false` if you want the earlier take instead.
`provenance` is there so the decision can be argued with rather than merely
observed: text-only disfluency detection tops out around F 86–91 %, so roughly one
in ten retake calls is wrong. That is why it is a flag and not a deletion.
