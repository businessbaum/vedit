---
name: captions
description: This skill should be used when the user asks for subtitles or on-screen text of what is being said - "brenn die Untertitel ein", "mach Untertitel", "Untertitel drauf", "leg Untertitel drüber", "brenn die Subs ein", "mach die Captions", "Wort für Wort Untertitel", "Karaoke-Untertitel", "korrigier den Untertitel-Text", "burn in the captions", "add subtitles", "burn the subs", "word-by-word captions", "fix a caption typo". Covers ASS generation, the pixel-width grouping rule for German, the font table, and burn-in through libass.
argument-hint: [project-slug] [preset]
arguments: [project, preset]
---

# captions

Generate an ASS subtitle file from the word-level transcript and burn it into the render.
Arguments: `$project` slug, `$preset` a name from `presets/captions/*.json`. Full request in
`$ARGUMENTS`. An empty `$`-placeholder means the skill auto-triggered rather than being
slash-invoked: take the project from `PIPELINE_STATE` in the session context and say which
one you picked. Verb names follow the build contract; `python engine/ve.py --help` wins.

## Two German-specific rules that decide whether this works

### 1. Never group by `max_chars`

German compound nouns are single unsplittable tokens that blow any character cap.
`Geschwindigkeitsbegrenzung` is 26 characters and occupies roughly **60% of a 1920 frame at
90 px**. A `max_chars: 22` rule cannot break it, so it either overflows the frame or the
grouper silently emits a line it promised not to.

Group by a **pixel-width budget** instead, with an auto-shrink rule for a single word that
exceeds the budget on its own. `estimate_text_width_px()` owns the measurement;
`group_words()` owns the budget. The details live in `docs/fonts.md` and in `engine/vedit/captions.py`'s module docstring.

This is the most common German caption failure and it is entirely avoidable.

### 2. The font must be a static single-weight file, named by its exact nameID 1

Variable fonts cannot be weight-selected from ASS. `InterVariable.ttf` renders
**byte-identical to Arial** through libass - same md5 - while logging that it loaded the file.
Exit 0, no warning. `Style: D,Inter,...,Bold=-1` gets you DirectWrite Arial and nobody notices
until the export is on YouTube.

Use only these, with the exact `Fontname` string and `Bold` value:

| file | ASS `Fontname` | `Bold` |
| --- | --- | --- |
| `Inter-Regular.ttf` | `Inter` | `0` |
| `Inter-Bold.ttf` | `Inter` | `-1` |
| `Inter-SemiBold.ttf` | `Inter SemiBold` | `0` |
| `Inter-ExtraBold.ttf` | `Inter ExtraBold` | `0` |
| `Montserrat-Bold.ttf` | `Montserrat` | `-1` |
| `Montserrat-ExtraBold.ttf` | `Montserrat ExtraBold` | `0` |

**`assets/fonts/` contains fonts only.** libass loads every file it finds there and errors on
anything else - a stray `README.md` produces `Error opening memory font 'README.md'`. That
directory is an `ask` permission in settings for exactly this reason; if a doc belongs with
the fonts it goes in `docs/fonts.md`.

## Step 1 - choose a preset

`presets/captions/*.json` holds style, grouping budget, position and highlight behaviour.
Pick by `output.platform`, not by taste:

- Vertical platforms want large, centred, 1-3 words, high in the safe area - the bottom third
  is under the platform's own UI.
- YouTube longform wants two lines, bottom-anchored, sized so a phone viewer can read it.

Confirm the choice with the user before generating, and name what changes.

## Step 2 - generate the ASS

```
python engine/ve.py captions $project --preset $preset
```

Two structural requirements, both of which fail silently when violated:

- The **23-field V4+ `Style: Format:`** line (`Name` .. `Encoding`, libass's own field list -
  the 24th field, `AlphaLevel`, is SSA v4 and not ASS V4+) and the **10-field Events
  `Format:`** line must be canonical, and the row under each must have exactly that many
  columns. A count mismatch makes libass exit 0, **prepend a stray comma into the rendered
  text, and silently drop outline and bold.** Reproduced with a 5-field Format against a
  6-field Dialogue. `captions.validate_ass()` checks both counts before any burn; it is not
  optional and `burn_in()` refuses on a failure.
- The file is UTF-8. German diacritics round-trip correctly through libass when it is -
  `Größe Straße Übermäßig äöüÄÖÜß` was verified end to end.

The sidecar is written to `projects/<slug>/out/captions.ass` and referenced from
`captions.sidecar_path`.

Timing comes from the same word list the cuts came from, mapped through the timeline
positions in `_computed`. Captions are therefore invalidated by any cut change - regenerate
after re-cutting rather than patching.

## Step 3 - fix the text, not the transcript

Numbers are the predictable problem. `--suppress_numerals` is mandatory at transcription, so
the transcript says `zwanzig` where the speaker said "20". Fix it in
`captions.overrides[]` as `{word_index, text}`, never by editing the transcript - the
transcript is the anchor for every word range in the project and rewriting it desynchronises
the cuts.

Same for proper nouns, product names and `GmbH`-style abbreviations. Collect them in one pass
by scanning for low-`score` words: alignment score is useless as a filler signal but it is a
good "the aligner was unsure what this word was" signal, which is exactly what a proper noun
looks like.

## Step 4 - burn in

```
python engine/ve.py render $project
```

Burn-in happens in the final mux, once, over the concatenated video - never per segment. A
per-segment burn re-renders the same subtitle at every boundary and lands a partial line on
each side of a cut.

The `subtitles=` and `fontsdir=` escaping is exact and non-obvious. Forward slashes
everywhere, and the value **quoted** with a single-escaped drive colon:

```
subtitles='C\:/Users/me/.../out/captions.ass':fontsdir='C\:/Users/me/.../assets/fonts'
```

`escape_filter_path()` in `vedit/ffmpeg.py` owns this and returns the string **including its
quotes** - do not add your own. Quoting, not nesting depth, is what decides: the bare
single-escaped form fails with `No option name near '/Users/...'`, and only the quoted form
survives a comma, semicolon or bracket in the path, all of which are legal on Windows.
`escape_filter_path_nested()` returns the doubled unquoted `C\\:/...` variant for anyone who
needs it, with that limitation documented.

All of it goes through an argv list from Python. The same path built in Git Bash becomes
`C\;C:\Program Files\Git\Users\...` - reproduced live, not theoretical.

Set `captions.enabled` and `captions.burn_in`, and set `stage` to `"captions"`.

## Step 5 - prove the font actually loaded

**QC check (e) is mandatory and this is the only way to catch silent fallback.** Render one
caption frame with the project style and one with an `Arial` control style, and assert the
hashes differ. Identical hashes mean libass fell back and the export will ship in the wrong
typeface.

Then also confirm: field counts on `Format:` lines, no line exceeding the pixel budget, and
no caption crossing a segment boundary in a way that leaves a fragment.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Captions look like Arial | Variable font, or wrong `Fontname` | Static file + exact nameID 1 string; run QC (e) |
| Stray comma in the text, no outline | Malformed `Format:` line | Emit canonical 24/10 field lines |
| `Error opening memory font 'README.md'` | Non-font file in `assets/fonts/` | Fonts only in that directory |
| One long word runs off the frame | Grouped by `max_chars` | Pixel-width budget plus single-word auto-shrink |
| `No option name near '/Users/...'` | `fontsdir` single-escaped | Double the backslash before the colon |
| Captions spell out numbers | `--suppress_numerals`, as designed | `captions.overrides` entries |
| Captions desync after a re-cut | Stale ASS | Regenerate; never patch timings by hand |
