---
name: intake
description: This skill should be used when the user asks to start a new video project or bring raw footage into the pipeline - "neues Projekt", "leg ein Projekt an", "lies das Material ein", "importier das Rohmaterial", "nimm die Clips auf", "normalisier die Dateien", "hier ist das Material", "mach was draus", "start a new project", "ingest the footage", "import this clip", "normalize the sources", "add this file to the project". Covers project creation, ffprobe survey, mandatory CFR transcode to the project timebase, and writing the sources block in project.json.
argument-hint: [project-slug] [source-path-or-folder]
arguments: [project, source]
---

# intake

Turn raw camera files into normalized sources the rest of the pipeline can trust. Intake is
the only stage allowed to touch original media, and it never writes to it.

Arguments: `$project` slug, `$source` file or folder. Full request in `$ARGUMENTS`. An empty
`$`-placeholder means the skill auto-triggered rather than being slash-invoked: take the
project from `PIPELINE_STATE` in the session context and say which one you picked. Verb
names below follow the build contract; if `python engine/ve.py --help` disagrees, the CLI
wins.

## The constraint that shapes this stage

**Every source is mandatorily transcoded to the project's CFR integer frame rate.** Not
recommended - required. Everything downstream assumes integer frames on one timebase, and
that assumption is only true because intake enforces it.

Why it is not optional:

- A 23.976 clip and a 30 clip in one project make `out_frame - in_frame` sums meaningless.
- ffprobe and other tools disagree on the frame count of VFR mp4s (113 vs 111 measured on a
  real phone clip). A source whose length is ambiguous cannot be cut accurately.
- Phone and screen-recorder output is VFR by default. This is the common case, not the edge.

The cost is one transcode per source at intake. The alternative is drift you discover at
export.

## Step 1 - create or open the project

```
python engine/ve.py new $project --preset longform
```

Format presets live in `presets/formats/*.json`: `longform`, `shortform-explainer`,
`tiktok-raw`. The preset fixes `timebase`, `output.resolution` and `output.platform`.

Ask which platform if it is not obvious. It is not cosmetic: `output.platform` drives the
loudness target and the caption safe area, and getting it wrong means re-mastering.
`youtube` -14 LUFS, and TikTok/Instagram/LinkedIn each differ - the numbers live in
`presets/formats/`, do not hardcode them here.

Pick the timebase from the dominant source's native rate. 30/1 for 30 or 60 fps material,
25/1 for 25 or 50. **Never pick a fractional rate** - 30000/1001 is representable but every
downstream second becomes a repeating decimal for no benefit. The user shoots for the web.

## Step 2 - survey before transcoding

For every candidate file:

```
python engine/ve.py probe <path>
```

Report a table: filename, duration, fps (and whether VFR), resolution, pixel format, video
codec, audio codec, sample rate, channels. Flag anything that will need attention:

| Finding | Consequence |
| --- | --- |
| `is_vfr: true` | Mandatory transcode (already mandatory, but say it) |
| Sample rate != 48000 | Resampled with soxr; audio-only, harmless |
| Mono audio | Fine, but captions/ducking assume dialogue is on the vo track |
| Rotation metadata | Must be baked in at transcode or the frame is sideways at export |
| Resolution below the project output | Upscaling; tell the user before doing it |
| Duration under 2 s | Probably not usable material - confirm |

Do not transcode until the user has seen this table. A 40 GB card copied in by accident is
cheaper to catch here than after an hour of encoding.

## Step 3 - normalize

```
python engine/ve.py intake $project "<abs-path>" --role acam
```

Roles: `acam` (the talking head), `broll`, `screen`, `music`. The role decides whether the
clip can be cut against the transcript and whether it is eligible to cover a J/L cut.

What the engine does per source, and what to verify afterwards:

- Transcode to the project CFR rate and sample rate, writing into `projects/$project/media/`.
- Audio to `pcm_s16le` at 48000. Dialogue is never re-encoded lossily before the master.
- Compute `strong_key` (sha256 of the whole file, memoized in `.cache/source_index.json`).
- `count_frames` with `ffprobe -count_frames`, which is authoritative - the container's
  `nb_frames` is a hint and is sometimes wrong.
- Write the `sources[]` entry with `path` **project-relative**, `original_path` absolute.

Then verify, do not assume: re-probe the normalized file and confirm `is_vfr` is false, fps
matches the project timebase exactly, and `duration_frames` is a plausible match for the
original duration. A transcode that silently produced 0 frames still exits 0.

## Step 4 - originals are read-only

`original_path` points at the user's media. That directory is denied to editing tools in
`.Codex/settings.json`. Never move, rename, delete or re-encode in place. If disk pressure
comes up, propose cache GC:

```
python engine/ve.py cache gc --max-gb 80 --keep-days 21
```

The `.cache/` tree is global and content-addressed, so two projects using the same B-roll
share its renders. Deleting it costs render time, never source material.

## Step 5 - set the stage and report

Set `stage` to `"intake"`, save, then report: number of sources, total normalized duration,
project timebase, output platform, and how much disk the normalized media took.

Then name the next step: transcribe the `acam` source.

## Multi-source notes

- Every source lands on the same timebase, so a multicam project needs no per-source rate math.
- Sync between cameras is not intake's job and is not automatic. If the user has two angles,
  say plainly that sync must be established (clap, or a shared timecode) before segments can
  reference both, and do not fake it.
- B-roll does not need a transcript. Add it with `--role broll` and leave it out of the
  transcribe stage.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Transcode exits 0, file has 0 frames | Rotation or codec edge case | Re-probe after every transcode; the check is cheap |
| `duration_frames` differs from the original by >1 s | VFR source with dropped timestamps | Re-transcode with an explicit `-vsync cfr`; report the delta |
| Frame count changes between two probes | `nb_frames` used instead of `-count_frames` | Only `count_frames()` is authoritative |
| Filter argument mangled to `C\;C:\Program Files\Git\...` | ffmpeg invoked through Git Bash | All ffmpeg calls go through an argv list from Python; never a shell string |
| Sideways video at export | Rotation metadata not baked in | Bake at intake, not at export |
