# projects: one folder per video

Everything about one video lives in one folder. Nothing about one video lives
anywhere else, with a single deliberate exception: rendered bytes, which are
content-addressed and shared globally in `.cache/`.

```
projects/
  _TEMPLATE/
    project.json          # a valid, empty long-form project. Copy it or read it.
  ep042/
    project.json          # the edit decision document, the only file you edit by hand
    media/                # CFR-normalized sources, written by intake
    out/                  # renders, captions.ass, exports
```

## Making a new project

```powershell
python engine\ve.py new ep042 --format longform
```

`ve new` calls `Project.create(root, slug, preset)`, which deep-merges
`presets/formats/<name>.json` over the built-in skeleton and writes
`projects/<slug>/project.json` plus empty `media/` and `out/`.

Copying `_TEMPLATE/` by hand works too. Change `project_id` and you have a
valid document. `_TEMPLATE` is not itself a project; the leading underscore
keeps it out of any `projects/*` glob.

## What lives where

| Path | Written by | Hand-editable |
| --- | --- | --- |
| `project.json` | every stage | yes, this is the point |
| `project.json._computed` | `Project.recompute()` on every load | no, it is overwritten |
| `media/` | intake | no |
| `out/` | render, captions, export | no |
| `../../.cache/` | render, transcription, graphics | no |

## Paths

Every path stored in `project.json` is **relative to the project directory**.
`Project.resolve()` turns it into an absolute path. That is what lets a project
folder be copied, renamed, or moved to another disk without rewriting anything
inside it.

The cache is the exception and is reached with `../../.cache/...`. It is global
and keyed purely on content, so two projects that use the same B-roll clip share
its renders instead of each paying for them. A path that resolved differently
per project would be a silent cross-project cache collision, which is why the
project-relative rule is not negotiable.

## The `_comment` convention

`project.json` and everything in `presets/` is parsed by the stdlib `json`
module, which has no comment syntax. Documentation therefore lives in string
values under keys that begin with an underscore:

```json
{
  "_comment_timebase": "Integer 30 fps, not 30000/1001. Intake normalizes to it.",
  "timebase": { "fps_num": 30, "fps_den": 1, "sample_rate": 48000 }
}
```

**Every consumer must ignore keys beginning with `_`**, with the one exception of
`_computed`, which is derived state rather than prose. Do not use JSONC, JSON5,
or trailing commas. The stdlib parser rejects all of them and the failure
surfaces as "project.json is not valid JSON" three stages later.

## What `project.json` actually is

An edit decision document, schema v1.1. Three invariants make it work:

1. **Integer frames on one timebase are the only authority.** `src_in_frames` is
   inclusive, `src_out_frames` is exclusive. Seconds exist only inside
   `_computed`, are regenerated on load, and are never read by the renderer.
2. **Timeline positions are derived**, never stored as truth. They are a running sum over
   enabled segments sorted by `order`, rewritten into `_computed` on every load.
3. **Nothing is ever deleted.** Rejected material becomes `"enabled": false` with
   a `reason` and `provenance`. Every automatic decision is one boolean away from
   being reversed, which is the only honest way to ship heuristics that are
   right about nine times in ten.

Read `_TEMPLATE/project.json` for the full field-by-field shape.

## Presets a project points at

`project.json` names four presets and freezes what it needs from them at
creation time:

| Field | Resolves to | Notes |
| --- | --- | --- |
| `format_preset` | `presets/formats/<name>.json` | merged in on `ve new`; supplies `output`, `timebase`, `captions`, `layout`, `hook`, `cut_profile` |
| `cut_profile` | a profile in `presets/cut-profiles.json` | `conservative` / `balanced` / `aggressive` |
| `captions.preset` | `presets/captions/<name>.json` | fonts, pixel-width budget, colours, margins |
| `output.platform` | a platform in `presets/platforms.json` | loudness target, aspect, caption safe area |

`output.loudnorm` is a **frozen copy** of the platform's target rather than a
live lookup. An already-mastered project must not silently re-target because
someone edited `platforms.json` afterwards; QC check (f) measures the rendered
master against the copy in the project.

## Calibration status

Every threshold in `presets/` is research-derived synthesis, not a perceptual
measurement. Nobody has yet watched a cut made with these numbers. So:

- every cut profile carries `"calibrated": false` and `"calibrated_against": null`
- every caption preset carries the same two fields
- every new project starts with top-level `"calibrated": false`
- QC warns while that flag is false

`presets/cut-profiles.json` documents, step by step, what calibration involves:
starting with recording a three-to-five-minute German fixture that contains a
deliberate retake, a long `ähm`, a topic change, and a compound noun long enough
to blow the caption width budget.

## Things that will bite

- **`media/` holds normalized sources, not originals.** Intake mandatorily
  transcodes everything to the project's CFR integer rate. Keep the camera files
  somewhere else; `sources[].original_path` records where they came from.
- **`_computed` is regenerated, not merged.** Anything you write there is gone on
  the next load.
- **`order` is authority for output sequence**, not array position. A story pass
  that reorders blocks changes `order` and leaves the array alone.
- **Deleting a segment is always wrong.** Set `enabled: false`.
