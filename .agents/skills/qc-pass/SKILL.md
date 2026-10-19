---
name: qc-pass
description: This skill should be used when the user asks whether a render or a project is technically sound - "prüf das", "check das mal", "ist das in Ordnung", "mach einen QC", "kontrollier das Ergebnis", "stimmt die Lautheit", "ist der Ton synchron", "hat das Video Fehler", "guck ob was kaputt ist", "run QC", "quality check this", "check the render", "is the audio in sync", "check the loudness", "did anything break". Runs the deterministic check suite over project.json and the rendered output and reports findings by severity.
argument-hint: [project-slug] [check-names]
arguments: [project, checks]
---

# qc-pass

Run the deterministic check suite. Arguments: `$project` slug, `$checks` an optional
comma-separated subset. Full request in `$ARGUMENTS`. An empty `$`-placeholder means the
skill auto-triggered rather than being slash-invoked: take the project from `PIPELINE_STATE`
in the session context and say which one you picked. Verb names follow the build contract;
`python engine/ve.py --help` wins.

## Run it

```
python engine/ve.py qc $project
```

Contract, used identically everywhere:

```
{"ok": bool, "findings": [{"severity", "code", "stage", "message", "evidence_path"}]}
```

Exit codes: **0 clean, 1 warnings, 2 failures.** Do not invent a fourth outcome and do not
report "looks fine" without an exit code behind it.

Every finding that can produce evidence does: a frame PNG, an `ebur128` log, a probe JSON.
`evidence_path` is what turns "audio is off" into something the user can check in ten seconds.

## The mandatory checks, and what each one actually catches

**(a) Frame count.** Output frame count equals the sum of `(out_frame - in_frame)` over
enabled segments. Catches OUT-rounding errors, a segment that rendered short, and a concat
that dropped a file. This is the check that catches the whole class of frame-math bugs, and
it costs one `ffprobe -count_frames`.

**(b) Audio sample count.** Equals `frames / fps * 48000`. Catches `adelay` in milliseconds
instead of samples, a wrong fade length, and per-segment audio concatenation - which drifts
about 10.7 ms per junction and is invisible until you watch a mouth.

**(c) Parameter match before concat.** Every segment's resolution, pixel format, codec,
profile, level, time base, sample rate and channel layout must agree, checked **before** any
`-c copy`. This is not paranoia: on a mismatch the concat demuxer produces a corrupt file
with **exit 0**. Nothing downstream will tell you.

**(d) Alpha probe.** One raw rgba pixel of a rendered graphic reads `a = 0`. Catches a
missing `--default-background-color=00000000`, a template that paints a background, and an
alpha-destroying codec (VP9-in-WebM silently discards alpha in this build).

**(e) Font fallback.** A caption render must hash **differently** from an Arial control
render. This is the only way to catch silent font fallback - libass logs that it loaded the
font, then falls back to DirectWrite Arial, exits 0, and produces a byte-identical result to
Arial. Verified. If this check is skipped, a wrong-typeface export ships.

**(f) Loudness.** `ebur128` on the master against the target for `output.platform`.
Integrated within 0.5 LU, true peak under the ceiling. Also catches the 192 kHz output from a
loudnorm pass 2 that lost its `-ar 48000`.

**(g) Chrome blank page.** A graphic screenshot must not equal the known-blank hash. Catches
a bad `file:///` URL, a JS error and a font that failed to load - all of which otherwise
produce a plausible file at exit 0. Three different HTML files once produced the identical
md5 of a blank error page.

## Project-level checks worth running before a render

These cost nothing and prevent the expensive failure:

- `_computed` was regenerated on load and was not hand-edited.
- Enabled segments have dense, non-colliding `order` values.
- No segment has `src_out_frames <= src_in_frames`.
- Every `overlays[].anchor` names a segment that exists and is enabled. An overlay anchored to
  a disabled segment is invisible and nobody will guess why.
- Every `word_range` lies inside the transcript.
- `calibrated` is `false` - report it as a warning, every time, until a real fixture has been
  run. It is not a defect, it is a statement about how much the thresholds can be trusted.
- Source paths in `sources[]` resolve, and `strong_key` still matches the file on disk. A
  changed source under an unchanged path invalidates every cached render.

## Reading the result

Severities:

- **failure** - the output is wrong. Do not ship, do not proceed to the next stage.
- **warning** - the output is probably fine but something is unverified or off-convention.
- **info** - measurements worth recording.

Report failures first, with the evidence path, then a one-line count of warnings. Do not
paginate a long finding list into the conversation: summarise by code and point at the JSON.

**Do not fix findings inside this skill.** QC reports. Fixing belongs to the stage that owns
the defect, and mixing the two means the checker starts agreeing with the code it just wrote.
Name the owning stage in the handoff: check (a) failures belong to cut-rough or export-final,
(e) to captions, (d) and (g) to gfx-html, (f) to export-final.

## When a fresh pair of eyes is warranted

For a final export before delivery, dispatch the `qc-inspector` agent rather than checking
inline. A fresh context has not just written the render command and is not invested in it
being correct. That is the whole reason the agent exists.

Inline is fine for a mid-pipeline spot check.

## Failure modes of QC itself

| Symptom | Cause | Fix |
| --- | --- | --- |
| QC passes, output is visibly broken | Checked the plan, not the file | Every check probes the actual output |
| Check (a) off by exactly the segment count | OUT treated as inclusive | `out` is exclusive; half-open `[in, out)` |
| Check (e) skipped as "cosmetic" | It is not | A wrong-typeface export is a re-export |
| Check (c) skipped because concat "worked" | Concat exits 0 on corrupt output | Never skip; exit 0 proves nothing here |
| Findings reported without evidence | `evidence_path` unset | A finding the user cannot verify is an opinion |
