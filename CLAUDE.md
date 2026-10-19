# Editing — a frame-exact video pipeline driven from Claude Code

This repo turns raw talking-head footage into a finished video by writing decisions
into one JSON document and rendering from it. The stages are **intake →
transcribed → story → rough_cut → graphics → captions → music → export**; each
reads `project.json`, does one thing, writes it back.

## The constraints that shape everything

**The main pipeline imports nothing outside the Python stdlib.** It runs on the
system Python 3.14.2, the only Python installed. Heavy ML lives in `.venv-asr`
(3.12), reached **only** via `subprocess`. Reach for numpy once and 3.14 wheel
availability is a permanent tax.

**Integer frames on ONE timebase are the only authority.** Intake *mandatorily*
transcodes every source to the project's CFR integer rate, killing the VFR,
drop-frame and mixed-rate bug classes at once. Seconds live only in `_computed`
and are never read by the renderer. `in` is inclusive, `out` is **exclusive**:

```python
in_frame  = math.floor(t_in  * fps + 0.5)   # half UP   -> ties land later
out_frame = math.ceil (t_out * fps - 0.5)   # half DOWN -> ties land earlier
if out_frame <= in_frame: out_frame = in_frame + 1
```

Get OUT backwards and every segment gains a frame: 200 cuts ≈ 6.7 s of drift.
Verified here — `trim=start=1.0:end=2.0` → 30 frames; `end=2.0166667` (the tie) →
**31**.

**Nothing is ever deleted.** Rejected material becomes `"enabled": false` with a
`reason` and `provenance` — one boolean from reversal.

**Every ffmpeg call is an argv list from Python `subprocess`** (never a shell
string, never Git Bash) and **every filtergraph goes to a file**
(`-/filter_complex fc.txt`), unconditionally.

**Every graphic is a pure function of `?t=<seconds>`.** No CSS animation, no GSAP,
no `requestAnimationFrame`: headless Chrome has no virtual clock, so anything
time-driven is sampled at an arbitrary moment and caching becomes a lie.

## `project.json` is the single source of truth

One file per project at `projects/<slug>/project.json`, schema v1.1. Renders derive
from it; **nothing ever derives it from a render.** Full schema and the worked
"move that cut 200 ms earlier" example: [docs/edl-schema.md](docs/edl-schema.md).

- `segments[].src_in_frames` / `src_out_frames` — integers, the only authority.
- `_computed` — a running sum over enabled segments, rewritten on every load.
  **Never hand-edit it; never read it as authority.**
- Overlays anchor to `{segment_id, offset_frames}`, captions to word indices;
  absolute positions break on every re-cut. Paths are project-relative.

**Cache-key rule.** A key covers only what changes the pixels: source strong key,
in/out frames, timebase, resolution, pix_fmt, speed, the *resolved* filtergraph,
encoder settings, tool versions. It must **never** contain timeline position,
segment id or index, anything about a neighbour, `enabled`, `reason`,
`provenance` or `locked` — put those in and every trivial edit nukes the cache.
Each artifact carries a `<key>.json` sidecar; diff two to see why something
re-rendered.

## On-screen text must add, never repeat

**If the voice is already saying it, do not also write it on screen.** A card
that spells out the sentence being spoken burns the only picture you have and
makes the result read as auto-generated. Use a text-free graphic, an image, or
nothing.

Text earns its place only for what the voice does *not* supply: names, numbers,
structure (counters, lists, logs), framing and punch-line setups. Word-synchronous
captions are exempt — they serve sound-off viewing, not illustration.

Worked right and wrong side by side: [docs/animation-toolkit.md](docs/animation-toolkit.md).

## How to run something

```powershell
.\tools\ve.ps1 new ep042 --preset longform --platform youtube
.\tools\ve.ps1 intake ep042 "%USERPROFILE%\Videos\raw\cam_a.MP4"
.\tools\ve.ps1 transcribe ep042 --language de
.\tools\ve.ps1 cut ep042 --dry-run
.\tools\ve.ps1 captions ep042            # optional: writes out\captions.ass
.\tools\ve.ps1 gfx ep042                 # optional: renders overlays[] to alpha .mov
.\tools\ve.ps1 render ep042 ; .\tools\ve.ps1 qc ep042
```

`render` is the only stage that writes the master. It composites rendered
overlays and then burns the ASS **inside that one command**, so `captions` and
`gfx` are things you run *before* it, never after — running them afterwards
leaves the master a version behind.

**Use the wrapper, not `python engine\ve.py`.** It puts
`.venv-asr\Lib\site-packages\torch\lib` on `PATH` in the *parent* process — the
entire cuDNN fix — and sets `PYTHONUTF8=1`.

`ve status <slug>` says where a project is and what comes next. `ve qc` exits **0**
clean / **1** warnings / **2** failures. The story pass sits between `transcribe`
and `cut` and is Claude's job, not the CLI's — [docs/pipeline.md](docs/pipeline.md).

## Verified environment facts

| Fact | Value |
| --- | --- |
| ffmpeg / ffprobe | `8.1.1-full_build-www.gyan.dev` — libx264, NVENC h264/hevc/av1, libass, libvmaf, soxr, whisper filter |
| GPU | RTX 4070 SUPER, 12282 MiB, driver 610.74 |
| RAM / disk | 32 GB; `C:` ~554 GB free (cache on `C:`; `G:` has 11.5 GB) |
| System Python | **3.14.2** at `C:\Python314` — the only one installed |
| uv | 0.9.28; CPython 3.12.12 and 3.13.11 already on disk |
| Node / npm | v24.13.0 / 11.6.2 |
| Chrome | `C:\Program Files\Google\Chrome\Application\chrome.exe` |

Also measured: `chrome --headless=new --screenshot` **398 ms/frame** at 1080p,
byte-identical for the same URL; `-ss` **before** `-i` is frame-accurate and 5.5×
faster than after `-i`.

**Not verified:** every cut threshold in `presets/cut-profiles.json` is research
synthesis, never tested on real footage. `"calibrated": false`, and QC check (h)
warns until that changes — [docs/cutting.md](docs/cutting.md).

## Gotchas — by symptom, because that is what you will see

Almost everything here fails **quietly**: exit 0, plausible file size, no warning.

| Symptom | Cause | Fix |
| --- | --- | --- |
| filter arg becomes `C\;C:\Program Files\Git\…` | Git Bash rewrites colon-args as POSIX PATH lists | argv from `subprocess`, never Git Bash |
| captions came out Arial, exit 0 | libass fell back silently; a variable font can never match an ASS weight axis | static single-weight file, exact `nameID 1`; QC (e) |
| caption reads `,0,0,0,,HALLO WELT` | malformed ASS `Format:` shifts every field, exit 0 | canonical 23-field Style + 10-field Events `Format:` |
| `No option name near '/Users/…'` | drive-letter colon in a filter arg | `escape_filter_path()` — returns the **quoted** `'C\:/Users/…'` *including its quotes*; do not add your own. (`escape_filter_path_nested()` gives the doubled unquoted `C\\:/…` variant, which cannot survive a comma or bracket in the path.) |
| graphic blank, all frames identical, exit 0 | Chrome screenshotted its own error page (`md5 64246983…`) | absolute Windows path in the `file://` URL; QC (g) |
| segments land a fraction of a frame off | `adelay` with no suffix takes integer **ms** | `adelay=<n>S` (samples) |
| voice lost ~6 dB when music arrived | `amix` defaults to `normalize=1` | `normalize=0`; measured −21.1 → −27.0 → −21.0 dB |
| master is 192 kHz | `loudnorm` silently resamples | `-ar 48000` on pass 2, always |
| output corrupt, wrong size, exit 0 | the concat demuxer does **not** validate inputs | `concat_compatible()` before every `-c copy`; QC (c) |
| audio drifts out of sync over the edit | audio was joined, not rendered in one pass | one `atrim`→`afade`→`adelay=<n>S`→`amix` pass; never cache audio per segment |
| filler detector eats proper nouns | WhisperX `score` is an **alignment** score | use it only as a timestamp-reliability flag (`< 0.35`) |
| compound noun overflows the frame | grouping by `max_chars` | pixel budget; `Geschwindigkeitsbegrenzung` at 90 px = **48 %** of 1920 |

Full symptom index: [docs/troubleshooting.md](docs/troubleshooting.md).

## Safety

- **Camera originals are never written to.** Intake reads them and writes a
  normalized copy into `projects/<slug>/media/`. Nothing may modify, move or
  delete a file under `%USERPROFILE%\Videos`.
- **Confirm before overwriting a render or an export** (`out/final.mp4` may be
  what the user already published) and **before `cache gc`** (a wrong `--max-gb`
  throws away hours of NVENC).
- **Never delete** a segment, a transcript, a cache sidecar, or a disabled
  segment's provenance; **never hand-edit `_computed`**; never fix a caption in
  the `.ass` — fix `captions.overrides` and regenerate.
- **Prefer `cut --dry-run` and `render --draft`** when the intent is to look
  rather than to ship.
