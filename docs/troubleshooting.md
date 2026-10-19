# Troubleshooting

Organised by **symptom**, because the symptom is what you actually see. Almost
everything in this pipeline that goes wrong goes wrong *quietly* — exit code 0,
plausible file size, no warning. That is why so many entries below end in "add the
QC check".

---

## Video and timing

### The video is a few seconds too long or too short, and the audio ends out of sync

**Cause.** The OUT rounding is wrong. `out` is **exclusive** and must round half
**down**; `in` is inclusive and rounds half **up**. Round OUT half up and every
segment gains one frame — 200 cuts is 200 frames ≈ 6.7 s.

VERIFIED at 30 fps: `trim=start=1.0:end=2.0` → 30 frames; `end=2.0166667` (exactly
the frame-60.5 tie) → **31** frames.

**Fix.** Use `Timebase.snap_pair()`. Never `round()`:

```python
in_frame  = math.floor(t_in  * fps + 0.5)
out_frame = math.ceil (t_out * fps - 0.5)
if out_frame <= in_frame: out_frame = in_frame + 1
```

**Catch it.** QC check (a): output frame count == Σ `(out − in)` over enabled
segments.

### The exported file is corrupt, or half of it is the wrong size — and ffmpeg exited 0

**Cause.** The concat demuxer does not validate its inputs. VERIFIED this pass:
joining a 1280x720 segment with a 640x360 segment exited **0**, produced 180
frames, and advertised 1280x720 for the whole file. No warning, nothing on stderr.

**Fix.** Call `concat_compatible()` before every `-c copy` join. It compares
`codec_name`, `width`, `height`, `pix_fmt` and `r_frame_rate` across all segments.

**Catch it.** QC check (c) — and it runs *before* the concat, not after.

### The concat step dies with `Line 1: unknown keyword '﻿file'`

**Cause.** The list file has a UTF-8 BOM. PowerShell 5.1's `Out-File -Encoding
utf8` writes one. VERIFIED this pass.

**Fix.** Write it from Python with `open(path, "w", encoding="utf-8")`, or from
PowerShell with `[IO.File]::WriteAllText(p, s, (New-Object Text.UTF8Encoding($false)))`.

### A cut lands on the wrong frame, or every segment is one frame long

**Cause.** Cut points not aligned to the frame grid. WhisperX timestamps are
arbitrary floats, so **every** cut point from a transcript hits this. Unsnapped
cut points make the concat filter accumulate 8.33 ms of A/V drift per cut —
measured at 1250 ms over 150 cuts. Snapping brings it to exactly 0 samples.

**Fix.** Snap before the graph is built, and emit seconds as
`f"{frames * fps_den / fps_num:.6f}"`.

### Stream-copy cutting includes footage that should not be there

**Cause.** `-c copy` snaps the in-point back to the previous keyframe. A 75-frame
request came back as 92 frames.

**Fix.** Never use `-c copy` to cut. It is only for remuxing a whole file or
joining segments produced with identical encode settings.

### ffprobe says the source has a different number of frames than another tool

**Cause.** The file is VFR. ffprobe and other tools genuinely disagree (113 vs 111
on the same file). A VFR file has no stable frame index.

**Fix.** This is what intake is for. Every source is transcoded to the project's
CFR integer rate on the way in, mandatorily. If a source got in without being
normalized, re-run intake on it. `nb_frames` is never trusted either — use
`ffprobe -count_frames`.

---

## Audio

### The voice got quieter as soon as music was added

**Cause.** `amix` defaults to `normalize=1` and divides by the number of inputs.

VERIFIED this pass, identical voice signal:

| | mean_volume |
| --- | --- |
| voice alone | −21.1 dB |
| `amix=inputs=2` | **−27.0 dB** |
| `amix=inputs=2:normalize=0` | −21.0 dB |

**Fix.** Always `normalize=0`, and control levels explicitly with `volume`.

### The master is 192 kHz

**Cause.** `loudnorm` silently resamples its output. VERIFIED this pass: pass 2
without `-ar` produced a 192000 Hz file; with `-ar 48000`, 48000 Hz.

**Fix.** `-ar 48000` on the loudnorm pass. Always.

### The audio drifts progressively out of sync over a long edit

**Cause.** Audio was joined instead of being rendered in one pass. AAC segments
carry encoder priming and drift ~10.7–30 ms per junction. Even lossless PCM
drifts: VERIFIED this pass, a 30-frame + 15-frame join gave **71480 samples
against an expected 72000 — 520 samples (10.8 ms) lost at one junction.**

**Fix.** Never cache or concatenate audio per segment. One `atrim` → `afade` →
`adelay=<n>S` → `amix=normalize=0` pass over the whole timeline. VERIFIED this
pass: 72000/72000 samples, duration exactly 1.500000 s.

**Catch it.** QC check (b): sample count == `frames / fps × 48000`.

### Segments land a fraction of a frame off, systematically

**Cause.** `adelay` without a suffix takes integer **milliseconds**, and timeline
positions at 30000/1001 are not integer ms.

**Fix.** `adelay=<n>S` — the `S` suffix takes samples. VERIFIED this pass:
`adelay=48001S` and `adelay=1000` differ by exactly one sample.

### Every cut clicks

**Cause.** A splice joins two samples that differ, creating a broadband step.
Measured: 3508 LSB against a 119 LSB baseline — 29.5× — on a hard mid-phase cut.
Room tone is never zero, so "it's a silent gap" is not a defence.

**Fix.** Both, belt and braces: snap to a zero crossing within ±5 ms
(audio-only, sub-frame, never moves the video), **and** crossfade 20 ms.

### The join is shorter than it should be

**Cause.** `acrossfade` defaults to `o=1` and shortens the join by exactly `d`.

**Fix.** This pipeline does not use `acrossfade`. Build splices from explicit
`afade` + `adelay` where the arithmetic is ours.

---

## Captions

### The captions rendered in Arial instead of the bundled font

**Cause.** libass could not match the `Fontname` and fell back to DirectWrite.
It exits 0 and logs nothing that reads as an error.

The usual reason is a **variable font**: `InterVariable.ttf` has `nameID 1 =
"Inter Variable"` and an `fvar` table, and ASS has no way to select a weight axis,
so even a correct family match gives you Regular plus faux-bold — never real
weight 800.

**Fix.** Static single-weight files only, with the exact `nameID 1` string:

| file | ASS `Fontname` | `Bold` |
| --- | --- | --- |
| `Inter-Regular.ttf` | `Inter` | `0` |
| `Inter-Bold.ttf` | `Inter` | `-1` |
| `Inter-SemiBold.ttf` | `Inter SemiBold` | `0` |
| `Inter-ExtraBold.ttf` | `Inter ExtraBold` | `0` |
| `Montserrat-Bold.ttf` | `Montserrat` | `-1` |
| `Montserrat-ExtraBold.ttf` | `Montserrat ExtraBold` | `0` |

**Catch it.** QC check (e) — render one caption frame and assert its hash differs
from an Arial control. VERIFIED this pass: `Inter ExtraBold` hashed
`9A811E44…` against the Arial control's `8FF29A31…`. An identical hash is the
failure.

### ffmpeg says `Error opening memory font 'README.md'`

**Cause.** libass loads **every file** in `fontsdir`. Confirmed in the verbose log
this pass — all six `.ttf`s were opened to render one word.

**Fix.** `assets/fonts/` contains fonts and nothing else. Documentation about the
fonts lives in `docs/`.

### The burned caption reads `,0,0,0,,HALLO WELT`

**Cause.** A malformed `Format:` line. A 5-field Events `Format:` against a
canonical 10-field `Dialogue:` makes libass shift every field: the surplus
fields leak into the visible text and the outline and bold are silently dropped.
**Exit code 0.** Reproduced and inspected this pass.

**Fix.** Always emit the canonical **23-field V4+ Style** `Format:` (`Name` ..
`Encoding`) and the **10-field Events** `Format:`. Assert the field counts match
before writing -- `vedit.captions.validate_ass()` does exactly that, and
`burn_in()` refuses on a failure. The 24th field some references list,
`AlphaLevel`, belongs to SSA v4 and is not part of ASS V4+.

### `Unable to parse "original_size" option value` / `No option name near '/Users/…'`

**Cause.** A Windows drive-letter colon inside a filter argument. The colon splits
the option and the backslashes are eaten.

**Fix.** Forward slashes plus an escaped colon:

| spelling | result |
| --- | --- |
| `fontsdir=C:/Users/…` | fails (VERIFIED this pass) |
| `fontsdir=C\\:/Users/…` | works (VERIFIED this pass) |
| `subtitles='C\:/path/subs.ass'` | works |
| `subtitles=C:\path\subs.ass` | fails |

Nested option values need the **doubled** backslash. Commas inside `force_style`
need `\,`.

### A German compound noun overflows the frame or breaks mid-word

**Cause.** Grouping by `max_chars`. `Geschwindigkeitsbegrenzung` is one
unsplittable 26-character token. VERIFIED this pass: rendered in Montserrat
ExtraBold at 90 px on a 1920-wide frame it measures **919 px — 48 % of the
width**.

**Fix.** Group by a **pixel-width budget**, with an auto-shrink rule for a single
word that still exceeds it. No character cap will ever be right for German.

### The captions spell out every number

**Cause.** `--suppress_numerals`, which is mandatory — wav2vec2's character
dictionary has no digits, so numerals otherwise arrive with no timestamps at all
and the cut algorithm `KeyError`s.

**Fix.** This is a known trade. Use `captions.overrides`:

```jsonc
"overrides": [{ "word_index": 1204, "text": "20" }]
```

---

## Graphics

### The graphic is a blank page, or all frames look identical

**Cause.** Chrome rendered *its own error page*, screenshotted that, and exited
**0** with a plausible file size. VERIFIED this pass — two different missing URLs
both produced `size=18097, md5=64246983D924E149DACCA9B3FFC5CC24`.

Usual reasons: a wrong path; a `file://` URL built from a Git Bash `$PWD` (use
`cygpath -m`); a template that threw before painting.

**Fix.** Build the URL from an absolute Windows path with forward slashes.

**Catch it.** QC check (g) — assert the frame is not a uniform page, and keep the
hash above as a fast pre-filter.

### The graphic covers the whole frame instead of compositing

**Cause.** No alpha. Either the template painted a background over
`--default-background-color=00000000`, or the encode dropped it. **VP9-in-WebM
does not carry alpha in this build** — it probes as `yuv420p` and every pixel
reads `a=255`, even with `-metadata alpha_mode=1`.

**Fix.** `html, body { background: transparent }`, and encode to `qtrle -pix_fmt
argb` (VERIFIED this pass: transparent pixel reads `0,0,0,0` after the round
trip) or ProRes 4444.

**Catch it.** QC check (d) — one raw rgba pixel must read `a=0`.

### The same graphic renders differently on two runs

**Cause.** The template animates over wall-clock time. Chrome's `--screenshot`
fires "when the page looks loaded" and there is no virtual clock, so a CSS
animation, a GSAP timeline or `requestAnimationFrame` is sampled at an arbitrary
moment. This makes content-addressed caching a lie and produces judder.

**Fix.** Every graphic is a pure function of `?t=<seconds>`. VERIFIED this pass:
a `t`-driven template screenshotted twice gave byte-identical PNGs.

### A template change had no effect

**Cause.** The cache key covers the template *path* but not its *content*.

**Fix.** Hash the template bytes into the key, along with props, `t`, `renderer`
and `renderer_version`. Then diff the two `<key>.json` sidecars to see which field
did or did not move.

### Rendering a graphic takes forever

Expected. VERIFIED this pass: **398 ms/frame** at 1920x1080. A 6-second graphic at
30 fps is ~72 s single-threaded. Cache per frame and parallelise four Chrome
processes with four separate `--user-data-dir` values.

---

## Transcription

### `Could not locate cudnn_ops64_9.dll` / `cublas64_12.dll`

**Cause.** The ctranslate2 wheel ships only the cuDNN loader stub and adds only
its own directory to the DLL search path. The five DLLs it then `LoadLibrary`s are
not in the wheel.

**Fix.** Put `<venv>\Lib\site-packages\torch\lib` on the DLL search path before
importing anything — torch 2.8.0+cu128 already contains all of them. Full
explanation in [setup.md](setup.md#cudnn). Preflight with
`ctranslate2.get_cuda_device_count()`.

### WhisperX installed but behaves strangely

**Cause.** It was installed on Python 3.14, where it cannot run, and pip silently
back-solved to whisperx 3.7.2 + torch 2.13 + transformers 5.15 — which imports and
fails at runtime.

**Fix.** ASR lives in `.venv-asr` on Python 3.12, and the version is always pinned
(`whisperx==3.8.6`) so a bad resolution fails loudly.

### `UnicodeDecodeError` reading the transcript JSON

**Cause.** WhisperX writes raw UTF-8 umlauts; Windows' default is cp1252.

**Fix.** `open(path, encoding="utf-8")`, always. `PYTHONUTF8=1` is set in
`.claude/settings.json` as a second line of defence.

### A word has no `start` and the cut pass crashes

**Cause.** Numerals, currency and `%` have no wav2vec2 character mapping.

**Fix.** `--suppress_numerals` (mandatory), and read defensively with
`w.get("start")`. Record which words were interpolated; never cut inside one.

### The filler detector removes proper nouns and keeps the `ähm`s

**Cause.** It is using WhisperX's `score`. That is an **alignment** score — low
means "the audio does not match these letters", which correlates with rare words
and proper nouns, the exact opposite of a filler.

**Fix.** Never use `score` as a filler signal. Its one legitimate use is a
timestamp-reliability flag: `score < 0.35` → widen padding 60 ms and forbid a cut
inside that word.

### `--diarize` fails after the transcription finished

**Cause.** It defaults to a gated HuggingFace model and 401s at the end.

**Fix.** For a single-speaker talking head, omit it. The JSON then has no `speaker`
key at all — do not write code that assumes one.

---

## Environment

### An ffmpeg filter argument arrives mangled with `C\;C:\Program Files\Git\…`

**Cause.** Git Bash. MSYS2 reads a colon-containing argument as a POSIX `PATH`
list, rewrites `:` to `;` and prefixes its own install root. Reproduced live this
pass: `fontsdir=C\\:/Users/…` arrived as
`fontsdir=C\;C:\Program Files\Git\Users\…`.

**Fix.** ffmpeg is launched from Python `subprocess` with an argv list. Never a
shell string. Never Git Bash. (`MSYS_NO_PATHCONV=1` also works, but relying on an
env var one forgotten shell can drop is not a rule.)

### PowerShell produced `Option not found` from a filter string

**Cause.** `"$SUBPATH:force_style=…"` — PowerShell parses `$SUBPATH:` as a drive
qualifier and expands it to an empty string.

**Fix.** Brace it: `"${SUBPATH}:…"`. Better: do not build filter strings in
PowerShell.

### ffmpeg fails to launch on a long edit with no useful error

**Cause.** Command-line overflow. A 150-segment `filter_complex` is 18,908
characters; ~260 segments exceeds the ~32767-char Windows limit.

**Fix.** `-/filter_complex fc.txt`, unconditionally.

### `drawtext` segfaults with `Fontconfig error: Cannot load default config file`

**Cause.** `font=Arial` triggers a fontconfig name lookup, which hard-crashes on
this machine (exit 139).

**Fix.** `fontfile=` with an explicit escaped path. libass is unaffected.

### A VMAF or metrics run reports the same score three times

**Cause.** The encode failed and the previous run's log JSON was still on disk and
got read as the new result.

**Fix.** Delete the sidecar before the run, and assert both the output file and
the fresh log exist afterwards. Applies to any ffmpeg step that writes a sidecar.

### `import torchcodec` fails with a scary pyannote warning

**Cause.** The installed ffmpeg 8.1.1 is a static build with no DLLs; torchcodec
0.7.0 only links FFmpeg shared majors 4–7.

**Fix.** Ignore it. WhisperX decodes via the ffmpeg CLI regardless.

---

## When nothing above matches

1. `.\tools\ve.ps1 status <slug>` — what stage, what is warm, what is next.
2. `.\tools\ve.ps1 qc <slug> --json` — every finding carries an
   `evidence_path`; open the artifact.
3. Diff the two `<key>.json` cache sidecars. When something re-rendered that
   should not have, or did not re-render when it should have, the answer is a
   single field that moved.
4. Check the exit code **and** `Test-Path` on the expected output. In this
   pipeline, exit 0 proves nothing.
