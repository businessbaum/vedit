# ffmpeg cookbook

`ffmpeg 8.1.1-full_build-www.gyan.dev` — libx264, NVENC (h264/hevc/av1), libass,
libvmaf, soxr, the `whisper` filter.

Every command here is marked:

- **VERIFIED** — executed on this machine and the result measured. The measurement
  is quoted.
- **UNVERIFIED** — correct as far as anyone knows, but not run here. Do not quote
  its numbers as fact.

Two rules apply to every single line below, without exception:

- **argv lists from Python `subprocess`.** Never a shell string. Never Git Bash.
  See [the mangling gotcha](#gitbash).
- **Filtergraphs go to a file**, passed as `-/filter_complex fc.txt`. Always, not
  only when long. Write the file without a BOM.

Seconds handed to ffmpeg are always `f"{frames * fps_den / fps_num:.6f}"`.

---

## Cutting

### The rough cut, one pass — VERIFIED

```
ffmpeg -y -i num.mp4 -/filter_complex fc.txt -map [v] -map [a]
       -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a pcm_s16le out.mkv
```

with `fc.txt` holding `trim`/`atrim` + `concat`. Frame-exact **and** sample-exact:
4 segments produced exactly 390 frames, 13.000000 s, and 624000/624000 audio
samples. All start/end values must already be snapped to the frame grid.

### One cache segment — VERIFIED

```
ffmpeg -y -ss 12.500000 -t 2.500000 -i source.mp4
       -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p -g 30
       -c:a pcm_s16le .cache/seg/xx/<key>.mkv
```

`-ss` **before** `-i` is frame-accurate here and 5.5× faster than after `-i`
(0.054 s vs 0.300 s seeking to 45.0 s, both landing on frame 1350).

Measured this pass: a 1.000000 s and a 0.500000 s segment cut this way gave
**30 and 15 frames** — exactly `out_frame − in_frame`.

> **UNVERIFIED:** frame accuracy of `-ss` before `-i` was only checked on
> H.264/MP4 with a 1-second GOP. Long-GOP, open-GOP and B-pyramid camera
> originals were not tested. Intake normalizes to `-g 30`, which sidesteps this.

### Never `-c copy` for cutting — VERIFIED

Stream-copy cutting snaps the in-point back to the previous keyframe. Measured
per-segment counts were 122/92/149/62 against targets of 120/75/135/60, and the
joined result was 425 frames instead of 390 (+1251 ms). `-c copy` is only ever
safe for remuxing a whole file or joining segments produced with identical encode
settings.

---

## Joining

### Guard first — VERIFIED

```
ffprobe -v error -select_streams v
        -show_entries stream=codec_name,width,height,pix_fmt,r_frame_rate
        -of json in.mkv
```

Measured this pass: joining a 1280x720 segment with a 640x360 segment exited
**0**, produced 180 frames, and advertised **1280x720** for the whole file. No
warning, no error, corrupt output. `concat_compatible()` exists because of this.

### Concat list file — VERIFIED

```
file 'C:\path\to\.cache\seg\b3\b3f7c1….mkv'
file 'C:\path\to\.cache\seg\7d\7d21ab….mkv'
```

Absolute Windows paths work with either slash direction.

> **Write it as UTF-8 without a BOM.** PowerShell 5.1's `Out-File -Encoding utf8`
> writes a BOM and ffmpeg fails with `Line 1: unknown keyword '﻿file'` (VERIFIED
> this pass, by accident). Use `[IO.File]::WriteAllText(path, text, (New-Object
> Text.UTF8Encoding($false)))` or, better, Python's `open(..., encoding="utf-8")`.

### Join — VERIFIED

```
ffmpeg -y -f concat -safe 0 -i list.txt -c copy joined.mkv
```

Measured this pass: 30-frame + 15-frame segments joined to **exactly 45 frames**.

Its audio did **not** survive as cleanly: the same join produced 71480 samples
against an expected 72000 — **520 samples (10.8 ms) lost over one junction**, with
lossless PCM. That is the empirical reason audio is never cached per segment.

---

## Audio

### The whole timeline in one pass — VERIFIED

```
[0:a]atrim=start=1.000000:end=2.000000,asetpts=PTS-STARTPTS,
     afade=t=in:st=0:d=0.02,afade=t=out:st=0.980000:d=0.02,adelay=0S:all=1[s0];
[0:a]atrim=start=3.000000:end=3.500000,asetpts=PTS-STARTPTS,
     afade=t=in:st=0:d=0.02,afade=t=out:st=0.480000:d=0.02,adelay=48000S:all=1[s1];
[s0][s1]amix=inputs=2:normalize=0:dropout_transition=0[out]
```

```
ffmpeg -y -i src.mkv -/filter_complex fc.txt -map [out] -c:a pcm_s24le -ar 48000 dialog.wav
```

Measured this pass: **72000 / 72000 samples, duration exactly 1.500000 s**,
`pcm_s24le`, 48000 Hz. There is no drift because there are no joins.

### `adelay` takes SAMPLES, with the `S` suffix — VERIFIED

Without a suffix `adelay` takes integer **milliseconds**, and timeline positions
are not integer ms. Measured this pass: `adelay=48001S` and `adelay=1000`
(= 48000 samples) produced outputs differing by exactly **1 sample**. Sample
granularity is only reachable through `S`.

### `amix` needs `normalize=0` — VERIFIED

Measured this pass, same voice signal in all three:

| | mean_volume |
| --- | --- |
| voice alone | **−21.1 dB** |
| `amix=inputs=2` (default `normalize=1`) | **−27.0 dB** |
| `amix=inputs=2:normalize=0` | **−21.0 dB** |

Adding a quiet music bed with default `amix` costs the voice **5.9 dB**, silently.

### Ducking — VERIFIED

```
ffmpeg -y -i voice.wav -i music.wav -/filter_complex fc.txt -map [out] -c:a pcm_s24le mix.wav
```

```
[0:a]asplit=2[v1][sc];[1:a]volume=-12dB[m];
[m][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400:makeup=1[mduck];
[v1][mduck]amix=inputs=2:duration=first:normalize=0[out]
```

Measured 15.6 dB of ducking under speech with full recovery in gaps. The first
input to `sidechaincompress` is the signal being compressed (music); the second is
the key (voice).

### Loudness, two passes — VERIFIED

```
ffmpeg -hide_banner -nostats -i voice.wav -af loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json -f null -
```

The JSON is the last `{…}` block on **stderr**. Substitute all five measured
values into pass 2:

```
ffmpeg -y -i voice.wav
       -af loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-29.95:measured_TP=-29.10:measured_LRA=0.00:measured_thresh=-40.14:offset=-0.05:linear=true
       -ar 48000 -c:a pcm_s24le voice_norm.wav
```

Hits the target exactly: a −30.0 LUFS input measured **−14.0 LUFS integrated,
−12.0 dBFS true peak** afterwards.

> **`-ar 48000` is mandatory.** Measured this pass: the identical command without
> it produced a **192000 Hz** file; with it, 48000 Hz.

Verify independently — never trust `loudnorm`'s own report:

```
ffmpeg -hide_banner -nostats -i master.wav -af ebur128=peak=true -f null -
```

### Extract audio for ASR — VERIFIED

```
ffmpeg -y -i input.mp4 -vn -map 0:a:0 -ac 1 -ar 16000 -c:a pcm_s16le -f wav whisper_in.wav
```

0.08 s for a 60 s file. With the high-quality resampler when downsampling 48k→16k:

```
-af aresample=resampler=soxr:precision=28:osf=s16:ochl=mono:osr=16000
```

### Silence detection — VERIFIED

```
ffmpeg -hide_banner -nostats -i speech.wav -af silencedetect=noise=-40dB:duration=0.3 -f null -
```

Parse **stderr** with `silence_start:\s*(-?[\d.]+)` / `silence_end:\s*(-?[\d.]+)`,
pair the Nth start with the Nth end, and clamp a trailing unmatched start to the
container duration. Boundaries matched ground truth exactly.

> The `ametadata=mode=print` file route **drops the final `silence_end`** when the
> file ends in silence — that value is emitted at EOF via `av_log`, not attached
> to a frame. Parse stderr.

---

## <a id="captions"></a>Captions

### Burn an ASS file — VERIFIED

```
ffmpeg -y -i in.mp4 -vf "subtitles=captions.ass:fontsdir=C\\:/Users/me/Editing/assets/fonts" -c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a copy out.mp4
```

Note the escaping, which is the part that goes wrong:

| spelling | result |
| --- | --- |
| `fontsdir=C:/Users/…` | **fails** — `No option name near '/Users/…'` (VERIFIED this pass) |
| `fontsdir=C\\:/Users/…` (doubled backslash, forward slashes) | **works** (VERIFIED this pass) |
| `subtitles='C\:/path/subs.ass'` (single-quoted, one backslash) | works |
| `subtitles=C\\:/path/subs.ass` (unquoted, doubled) | works |
| `subtitles=C:\path\subs.ass` (raw) | fails — the colon splits the option, backslashes are eaten |

Commas inside `force_style` must be escaped as `\,`.

### The font actually loaded — VERIFIED

Measured this pass, same text and same 1280x720 black frame:

| style | md5 of the rendered PNG |
| --- | --- |
| `Fontname: Arial`, `Bold: -1` (control) | `8FF29A31D0F4A725E20044F545C1E7AA` |
| `Fontname: Inter ExtraBold`, `Bold: 0` | `9A811E44F27B61745BEF326C774E3D0F` |

Different hash means the bundled font was used. **Identical hash means libass
silently fell back to Arial** — it exits 0 either way and logs nothing useful.
This is QC check (e).

libass loads **every file** in `fontsdir` (confirmed in the verbose log: all six
`.ttf`s were opened for a one-word caption). That directory must contain fonts
only; a stray `README.md` produces `Error opening memory font`.

### Group by pixel width, never `max_chars` — VERIFIED

`Geschwindigkeitsbegrenzung` at 90 px, measured two independent ways this pass:

| font | libass render | `estimate_text_width_px()` | share of 1920 |
| --- | --- | --- | --- |
| Montserrat ExtraBold | **919 px** | 924 px | **48 %** |
| Inter ExtraBold | — | 1112 px | **58 %** |

The rendered measurement and the metric read out of the TTF's own `hmtx`/`cmap`
agree to 0.5 %, so the budget can be computed without rendering. One unsplittable
token eating half the frame is why no character cap can be right for German.

### German renders correctly — VERIFIED

`Größe Straße Übermäßig` in `Montserrat ExtraBold` at 64 px with `Outline: 5`
rendered with correct umlauts, correct ß, and a visible outline (inspected this
pass). The `.ass` file must be UTF-8.

### A malformed `Format:` line corrupts silently — VERIFIED

A 5-field `Format: Layer, Start, End, Style, Text` against a canonical 10-field
`Dialogue:` line: ffmpeg **exits 0**, and the frame renders

```
,0,0,0,,HALLO WELT
```

— the surplus fields leaked into the visible text and the outline was dropped.
Inspected this pass. Always emit the canonical **23-field V4+ Style** `Format:`
(`Name` .. `Encoding`, libass ass.c style_fields; the 24th field `AlphaLevel` is
SSA v4, not ASS V4+) and **10-field Events** `Format:`. What actually breaks is a
COUNT MISMATCH between a Format line and its rows -- libass matches by name and
ignores an unknown extra column, so 24 renders identically, but a row with the
wrong number of columns does not.

### `drawtext` — VERIFIED

`font=Arial` (fontconfig name lookup) **segfaults** on this machine, exit 139.
Only `fontfile=` with an explicit escaped path works. libass is unaffected; it
resolves Windows fonts natively through DirectWrite.

---

## Graphics

### Alpha carriers — VERIFIED

```
ffmpeg -y -framerate 30 -i frames\f%04d.png -c:v qtrle -pix_fmt argb overlay.mov
```

Measured this pass: 10 frames of 1920x1080 → `qtrle, 1920, 1080, argb, 10`, and a
transparent pixel read back as **`0,0,0,0`** after the round trip.

| carrier | alpha | note |
| --- | --- | --- |
| `qtrle` `-pix_fmt argb` | **yes** | 13× smaller than ProRes on flat graphics — the default |
| `prores_ks -profile:v 4444 -pix_fmt yuva444p10le` | **yes** | yields `yuva444p12le`, which is correct |
| PNG/rgba sequence | **yes** | the intermediate |
| VP9 in WebM | **NO** | probes as `yuv420p`, every pixel `a=255`, even with `alpha_mode=1` |

### Prove alpha survived — VERIFIED

```
ffmpeg -y -i overlay.mov -vf "format=rgba,crop=1:1:10:10" -frames:v 1 -f rawvideo -pix_fmt rgba probe.raw
```

Four bytes. `a=0` means alpha survived; `a=255` means it was lost. This is QC
check (d).

### Composite an overlay — VERIFIED

```
[1:v]format=rgba,scale=960:540,setpts=PTS-STARTPTS+2/TB[gfx];
[0:v][gfx]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass:format=auto[v]
```

`eof_action=pass` keeps the footage running after the graphic ends.

> `drawbox` silently stops honouring `replace=1` when `x` or `y` is an expression,
> so animated boxes never write alpha and the output is fully transparent. Use a
> real renderer for generated alpha.

---

## Encoding

### NVENC H.264 — VERIFIED

```
ffmpeg -y -i joined.mkv -c:v h264_nvenc -preset p6 -tune hq -rc vbr -cq 20 -b:v 0
       -profile:v high -pix_fmt yuv420p -movflags +faststart out.mp4
```

`-b:v 0` is required or `-cq` is ignored. On a 20 s 1080p30 source: 2.6 s
(233.6 fps) vs libx264 `-preset medium -crf 20` at 5.7 s (105.8 fps).

| encoder | 2 Mbps | 4 Mbps | 8 Mbps |
| --- | --- | --- | --- |
| libx264 medium | 73.03 | 74.44 | 75.37 |
| h264_nvenc p7 multipass | 72.84 | 74.43 | 75.43 |
| hevc_nvenc p6 | 72.93 | 74.55 | 75.50 |

> **UNVERIFIED:** those VMAF numbers come from *synthetic* sources (mandelbrot,
> testsrc2). The relative ranking is meaningful; the absolute values and the
> crossover bitrate must be re-measured on real camera footage before they set a
> delivery bitrate.

HEVC needs `-tag:v hvc1` to be accepted by Apple players. `av1_nvenc` muxes into
MP4 as `av01`.

### Highest-quality master — VERIFIED

```
-c:v h264_nvenc -preset p7 -tune hq -rc vbr -multipass fullres -b:v 8M -maxrate 12M -bufsize 16M
```

### Do not use `-hwaccel cuda` — VERIFIED

290 fps software vs 295 fps `-hwaccel cuda` vs 298 fps with
`-hwaccel_output_format cuda`. No meaningful benefit, and keeping frames in GPU
memory puts them out of reach of `overlay`, `subtitles`, `drawtext` and `trim`.
NVENC is for the encode side only.

> **UNVERIFIED:** consumer NVIDIA drivers have historically capped concurrent
> NVENC sessions. All encodes here ran sequentially. If segment rendering is ever
> parallelised, test this first.

### Lossless remux — VERIFIED

```
ffmpeg -y -i in.mp4 -map 0 -c copy out.mkv
```

0.07 s for a 42 MB file, video packet MD5 identical before and after. Prove it
with `ffmpeg -v error -i f -map 0:v -c copy -f md5 -`.

---

## Inspection and QC

### Authoritative frame count — VERIFIED

```
ffprobe -v error -count_frames -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 in.mkv
```

`nb_frames` is absent in MKV and wrong often enough elsewhere. This decodes; it is
slow; it is right.

### Filmstrip at chosen frames — VERIFIED

```
ffmpeg -y -i in.mp4 -vf "select='eq(n\,90)+eq(n\,375)+eq(n\,912)',scale=400:-1,tile=3x1:margin=6:padding=6:color=0x202020" -frames:v 1 -update 1 filmstrip.png
```

Ideal for eyeballing every cut junction in one image. Note the escaped commas
inside `eq()`.

### Contact sheet — VERIFIED

```
ffmpeg -y -i in.mp4 -vf "fps=1/3,scale=320:-1,tile=5x4:margin=8:padding=6:color=0x202020" -frames:v 1 -update 1 contact.png
```

### VMAF — VERIFIED

```
ffmpeg -y -i distorted.mp4 -i reference.mkv -lavfi "[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS[r];[d][r]libvmaf=n_threads=16:log_fmt=json:log_path=vmaf.json" -f null -
```

> Delete `vmaf.json` before every run and assert it exists afterwards. A failed
> encode otherwise leaves the previous run's log on disk and it is read as the new
> result — this produced three identical scores before it was caught. The rule
> generalises to any ffmpeg step that writes a sidecar.

---

## Windows traps

### <a id="gitbash"></a>Git Bash mangles filter arguments — VERIFIED

Reproduced live this pass. The identical argv that works from PowerShell:

```
fontsdir=C\\:/Users/me/Editing/assets/fonts
```

arrives at ffmpeg from Git Bash as

```
fontsdir=C\;C:\Program Files\Git\Users\me\Editing\assets\fonts
```

MSYS2 treats the string as a POSIX `PATH` list, rewrites `:` to `;` and prefixes
its own install root. This is a shell bug, not an ffmpeg bug. **Rule: ffmpeg is
launched from Python `subprocess` with an argv list, never through Git Bash.**
(`MSYS_NO_PATHCONV=1` / `MSYS2_ARG_CONV_EXCL='*'` also work, but relying on an env
var that one forgotten shell can drop is not a rule.)

### PowerShell eats `$VAR:` — VERIFIED (research)

`"$SUBPATH:force_style=…"` makes PowerShell parse `$SUBPATH:` as a drive
qualifier and expand it to an empty string, producing the misleading ffmpeg error
`Option not found`. Brace it: `"${SUBPATH}:force_style=…"`. Better: do not build
filter strings in PowerShell at all.

### Command line length — VERIFIED

A 150-segment `filter_complex` is 18,908 characters; at ~126 chars per segment,
~260 segments overflows the ~32767-char Windows limit and the process fails to
launch with no useful error. `-/filter_complex fc.txt` is used unconditionally.
The legacy `-filter_complex_script` still works in 8.1.1.

### Never inline arithmetic in an argument string — VERIFIED (research)

A PowerShell inline expression once produced a literal `+M` filename; the encode
never ran and the loop still reported plausible numbers. Compute into a named
variable, then check the exit code **and** `Test-Path` on the expected output
after every invocation.

---

## Known gaps

- Whether ffmpeg can **decode** an alpha WebM produced elsewhere. Only the encode
  direction was tested.
- Long-form behaviour. The longest source tested was 60 s. Drift and timestamp
  behaviour on a 30–60 minute recording are unmeasured.
- Cost of the one-pass filter approach when 150 segments are spread across a
  45-minute file. `trim` decodes the source once per graph, so it may be far
  slower than the cache path on seek-heavy material.
- ASS rendering for non-Latin scripts, RTL text and complex shaping.
