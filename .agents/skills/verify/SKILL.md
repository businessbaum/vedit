---
name: verify
description: This skill should be used when the user asks to confirm a change to this repository is sound, and after any edit to engine/, presets/ or .Codex/ - "prüf ob das noch geht", "hab ich was kaputt gemacht", "verifizier das", "check ob das stimmt", "läuft das noch", "teste das mal", "stimmt das so", "verify this", "does this still work", "did I break anything", "check my change", "run the verification". Defines how THIS project verifies a change: the stdlib-only invariant, the frame-math fixtures, the ffmpeg argv rule, the scaffold checks and the end-to-end smoke render.
---

# verify

How this repository proves a change is sound. Full request text is in `$ARGUMENTS`.

This is the project-specific verification contract. Video output quality is a different
question - that is qc-pass, which probes rendered files. This skill checks that the code and
the scaffold still hold their invariants.

Run the tier that matches the change. Do not run the smoke render for a docs edit, and do not
skip it for a change to the render path.

## Tier 0 - always, and it takes seconds

**The stdlib-only invariant.** The main pipeline imports nothing outside the Python 3.14
standard library. No numpy, no pillow, no requests. Heavy ML lives in `.venv-asr` and is
reached only through `subprocess`.

```
python -c "import sys; sys.path.insert(0,'engine'); import vedit"
```

If that fails on an import, the invariant is broken. It is not a small break: the moment
someone reaches for a third-party package, Python 3.14 wheel availability becomes a permanent
tax on every future change, and 3.14 is the only interpreter installed.

Grep for the usual suspects across `engine/`: `numpy`, `PIL`, `requests`, `cv2`, `scipy`,
`pydantic`, `torch`. A hit inside `engine/` is a failure. A hit inside a `.venv-asr` sidecar
script is fine.

**The ffmpeg argv rule.** Every ffmpeg and ffprobe invocation goes through an argv list from
`subprocess` - never a shell string, never `shell=True`, never Git Bash. Git Bash mangles
`C:` inside filter arguments into `C\;C:\Program Files\Git\...`. This is reproduced, not
theoretical. Grep `engine/` for `shell=True` and for `os.system`; both are failures.

## Tier 1 - the frame math, whenever timebase, cut or render changed

The frame math is the one place where a small error compounds into a visibly broken video, so
it gets explicit fixtures rather than a reading.

Assert, for the project timebase:

- `snap_in` rounds half **up** - ties land later.
- `snap_out` rounds half **down** - ties land earlier.
- `out <= in` is corrected to `in + 1`.
- `out` is **exclusive**. `[in, out)` half-open is what makes concatenation exact.
- `secs()` emits `"%.6f"` under an invariant culture. A comma decimal separator produces an
  ffmpeg argument that parses as two arguments.
- `frames_to_samples` is exact at 48000 for the project rate.

The regression that matters most: a half-frame overshoot buys a whole extra frame (31 instead
of 30) if OUT rounds up. The inverse error - OUT treated as inclusive - makes every segment
one frame long, which is 6.7 s of drift over 200 cuts.

Then verify against ffmpeg rather than against the implementation's own belief: cut a
generated source at a known frame, `ffprobe -count_frames` the result, and assert the count
equals `out - in`. Test media is one command away:

```
ffmpeg -f lavfi -i testsrc2=size=640x360:rate=30:duration=10 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=10 -c:v libx264 -pix_fmt yuv420p -c:a pcm_s16le <tmp>/fixture.mkv
```

Generate fixtures into the scratchpad, never into `projects/` or `assets/`.

## Tier 2 - the scaffold, whenever `.Codex/` changed

```
Codex doctor
```

Zero "Invalid settings" lines is the pass condition. A single backslash in a Windows path
makes the JSON invalid and **the entire settings file is discarded** - permissions, env and
hooks all silently vanish, and `doctor` reports only "Invalid or malformed JSON" with no line
number. Use forward slashes.

`doctor` does not catch everything. Two things it misses, both verified on 2.1.218:

- **`Write(path)` permission rules are inert.** Only `Edit(path)` rules are matched by file
  permission checks; `Edit` covers all file-editing tools. `doctor` stays silent; a live
  session prints the warning. So also run one throwaway prompt and read the warnings:
  `Codex -p "Reply with exactly: OK" --model haiku`
- **Project `permissions.allow` and `additionalDirectories` are ignored until the workspace
  trust dialog has been accepted once.** The allowlist looks correct and does nothing.

Hook changes get exercised directly, not assumed:

```
echo '{}' | powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .Codex/hooks/session-state.ps1
```

Pass condition: exit 0, one line of valid JSON on stdout, no BOM, empty stderr. Test the
degenerate cases too - no `projects/` directory, an empty one, and a malformed `project.json`.
A hook that throws costs the user a session.

Skill and agent changes: the `description` is the only thing the model sees before deciding
to trigger. If it changed, re-read it as a stranger would and ask whether the literal phrase
the user actually types appears in it. The user writes German.

## Tier 3 - end to end, whenever the render path changed

Build a short synthetic project and take it through intake, cut, render, concat, audio and
mux, then run qc-pass. Fifteen seconds of generated source with a burned-in frame counter is
enough, and reading the burned-in numbers back out of the result is the only verification
that cannot be fooled by a plausible-looking file.

**Pass condition is `0 failure(s)`, not exit 0.** A correct repo exits **1**, because check
(h) warns on `calibrated: false` and will keep doing so until the German fixture exists. Read
the failure count, not the exit code, or this tier reports a permanent false negative.

The full chain, if the project has captions or graphics, is
`cut` -> `captions` -> `gfx` -> `render` -> `qc`: `ve render` is the only stage that writes
the master, and it composites the overlays and burns the ASS at the end of that one command.

This tier catches what unit tests structurally cannot: a concat that exits 0 on a corrupt
file, an audio graph that drifts only across joins, and a cached segment that no longer
matches its key.

## What "verified" means in this repo

Match the discipline of the existing docs: **mark every claim verified or unverified, and say
how.** "Should work" is not a result. Report what was executed and what it printed.

If a threshold in `presets/` has not been checked against real footage, it is unverified no
matter how reasonable it looks - `project.json` carries `"calibrated": false` for exactly
that reason, and the session hook surfaces it every session.

## Report format

State the tier run, each check with its measured result, and the failures first. If a tier was
skipped, say which and why. A verification that does not say what it did not check is not a
verification.
