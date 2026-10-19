---
name: export-final
description: This skill should be used when the user asks for the finished video file - "exportier das", "render das fertig", "mach das final", "renderte das Video", "mach den Endexport", "bau das Video zusammen", "mach es fertig für YouTube", "gib mir die MP4", "export the final", "render the final cut", "make the master", "export for TikTok", "give me the mp4". Covers segment rendering with the cache, guarded concat, the one-pass audio, loudness normalisation and the single mux.
argument-hint: [project-slug] [platform]
arguments: [project, platform]
---

# export-final

Assemble the finished file. Arguments: `$project` slug, `$platform` overrides
`output.platform` if the user wants a second deliverable. Full request in `$ARGUMENTS`. An
empty `$`-placeholder means the skill auto-triggered rather than being slash-invoked: take
the project from `PIPELINE_STATE` in the session context and say which one you picked. Verb
names follow the build contract; `python engine/ve.py --help` wins.

## The render architecture, which is settled

- **Video**: one cached MP4/MKV per segment, identical encode parameters, each starting on an
  IDR frame, then joined with `-f concat -safe 0 -c copy`. Verified lossless and near-instant.
- **Audio**: one continuous pass over the whole timeline into `pcm_s24le`. Never cached per
  segment.
- **Mux once**, encoding AAC exactly once.

Do not redesign this at export time. In particular, do not "simplify" it into a single
`filter_complex` over all segments: a 150-segment graph is about 19k characters and around
260 segments overflows the Windows command line, producing an unlaunchable process rather
than a helpful error. It also throws away the per-segment cache, which is the reason a second
export takes seconds.

**Filtergraphs always go to a file** and are passed as `-/filter_complex fc.txt`.
Unconditionally, not only when they get long.

## Step 1 - preflight

Before encoding anything:

1. Run qc-pass. An export over a project that fails QC wastes the longest operation in the
   pipeline.
2. Confirm `output.platform`. It drives the loudness target, the resolution and the caption
   safe area. If `$platform` differs from `output.platform`, do not silently mutate the
   project - render a second output path and say which one is which.
3. Report the plan: segment count, expected total frames, expected duration, codec, and
   whether captions and graphics are burning in this pass.

## Step 2 - render segments

```
python engine/ve.py render $project
```

Cached per segment under `.cache/seg/`, keyed on the source strong key, in/out frames,
filters, encoder and tool versions. Only changed segments re-render. `--only seg_0012`
renders one for inspection.

Encoder defaults: `h264_nvenc`, `cq 20`, `preset p6`. NVENC on this Ada card is at rough VMAF
parity with libx264 - behind by 0.19 VMAF at 2 Mbps, ahead by 0.06 at 8 Mbps - while running
2-4x faster. It is the right default for previews and a defensible one for finals. Switch to
libx264 only if the user asks for maximum quality per bit and is willing to wait.

Both encoders were byte-reproducible run to run on this machine, which is what makes
content-addressed segment caching safe.

## Step 3 - concat, but guard it first

**`concat_compatible()` runs before every `-c copy` concat, without exception.** On a
parameter mismatch the concat demuxer hands you a corrupt file with **exit 0** - no warning,
no error, a file that plays for ten seconds and then falls apart. QC check (c) is this check.

It compares resolution, pixel format, codec, profile, level, time base, sample rate and
channel layout across every segment. A mismatch means a segment was rendered with different
parameters - usually a stale cache entry from before an encoder setting changed. Re-render
the offenders rather than forcing the concat.

## Step 4 - audio and loudness

```
python engine/ve.py render $project
```

Then two-pass `loudnorm` to the platform target from `output.loudnorm` (YouTube: I -14,
TP -1.5, LRA 11; other platforms differ and live in `presets/formats/`).

**Pass 2 must carry an explicit `-ar 48000`.** Without it the output is silently 192 kHz -
loudnorm's internal rate leaks into the output, the file is four times larger than expected,
and some players resample it badly. This is the single most missed flag in the whole chain.

Two-pass, not single-pass: single-pass loudnorm is a dynamic estimate and lands a decibel or
more off target on material with a quiet open, which is every talking-head video with a bed.

## Step 5 - mux once

Steps 2 to 5 are all `ve render $project`: it renders or reuses the cached segments, concats
them, renders the audio in one pass, loudnorms it, and muxes AAC 320k exactly once into
`projects/$project/out/final.mp4` per `output.path`.

Do not re-encode video at the mux. If something needs to change in the picture, it changes at
the segment level and the cache handles the rest.

After the mux, `ve render` composites the rendered overlays and then burns the ASS if
`captions.burn_in` - in that order, so captions sit above graphics. Both re-encode the
master, which is why `ve gfx` and `ve captions` are things you run *before* `ve render`, not
after. Running them after leaves a master one version behind and nothing warns you.

`ve export` is a separate, later step: it runs full QC, refuses on a failure unless you pass
`--force`, then stamps a dated delivery copy and writes a `.manifest.json` beside it.

## Step 6 - verify the deliverable, then hand it over

Run the mandatory checks against the actual output file, not against the plan:

- Frame count equals the sum of `(out_frame - in_frame)` over enabled segments. QC (a).
- Audio sample count equals `frames / fps * 48000`. QC (b).
- `ebur128` integrated loudness within 0.5 LU of the platform target, true peak under the
  ceiling. QC (f).
- Container, resolution, frame rate and duration match `output`.
- Spot-check three frames: first, a segment boundary in the middle, last. A black frame at a
  boundary means a segment rendered short.

Then report: output path, file size, duration, measured LUFS and true peak, frame count,
segment count and how many segments came from cache. Set `stage` to `"export"`.

## Second deliverables

A vertical cut is not a re-crop of the finished file. It is a different `output.platform`,
which means a different loudness target, a different caption preset and a different safe
area, and usually a different story pass. Say that rather than scaling the 16:9 master.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Output plays then breaks up, ffmpeg exited 0 | Parameter mismatch in concat | `concat_compatible()` before every concat; re-render offenders |
| Master is 192 kHz | loudnorm pass 2 missing `-ar 48000` | Add it and re-run pass 2 |
| Encode process fails to launch | Filtergraph on the command line | Always write `fc.txt` and pass `-/filter_complex` |
| Loudness a decibel off target | Single-pass loudnorm | Two-pass |
| Export is slower every time | Cache invalidated by a changed encoder setting | Expected; confirm the setting change was intended |
| Black frame at a cut | Segment rendered short | Re-render that segment; check QC (a) |
| Voice quieter in the final than the preview | `amix` missing `normalize=0` | Fix in the audio graph, re-render audio |
