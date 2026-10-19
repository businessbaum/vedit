# First-run setup

Most of this machine is already set up. This page covers what is verified present,
the one thing that must be installed (the ASR sidecar), and the one thing that
will fail without an explanation (cuDNN).

## Already present — VERIFIED

| tool | version | location |
| --- | --- | --- |
| ffmpeg / ffprobe | `8.1.1-full_build-www.gyan.dev` | winget: `Gyan.FFmpeg` |
| Python | **3.14.2** — the only Python installed | `C:\Python314` |
| uv | 0.9.28 | on PATH |
| Node / npm | v24.13.0 / 11.6.2 | on PATH |
| Chrome | current | `C:\Program Files\Google\Chrome\Application\chrome.exe` |
| GPU | RTX 4070 SUPER, 12282 MiB, driver 610.74 | |
| RAM / disk | 32 GB; `C:` ~554 GB free | |

`uv` already has **CPython 3.12.12 and 3.13.11 downloaded locally**, so creating
the ASR venv does not download an interpreter.

The ffmpeg build has everything the pipeline needs: libx264, NVENC
(h264/hevc/av1), libass, libvmaf, soxr, and the `whisper` filter.

## The short version

```powershell
cd <repo>
.\tools\setup.ps1 -DryRun     # prints the entire plan, executes nothing
.\tools\setup.ps1             # does it; idempotent, safe to re-run
```

Everything below is what that script does and why, so that when a step fails you
can tell which one and what it was for.

## Nothing to install for the main pipeline

The main pipeline is **stdlib-only Python 3.14**. There is no `requirements.txt`,
no virtualenv, no `pip install`. It runs as-is:

```powershell
.\tools\ve.ps1 --help
```

Use `tools\ve.ps1`, not `python engine\ve.py`. The wrapper exists for three lines
of environment the Python process cannot set for itself: the torch DLL directory
on `PATH` (the cuDNN fix below — it must be set in the **parent** process, because
the loader reads `PATH` at DLL bind time), `PYTHONUTF8=1`, and being PowerShell
rather than Git Bash. It forwards everything else verbatim, exit code included.

If something in `engine/vedit/` ever needs a third-party package, that is a design
error, not a dependency — see [CLAUDE.md](../CLAUDE.md).

---

## The ASR sidecar

WhisperX cannot run on this machine's Python. WhisperX 3.8.6 declares
`requires-python <3.14,>=3.10` and hard-pins `torch~=2.8.0`, which has no cp314
wheels. So it lives in its own venv and is reached only through `subprocess`.

> **Never let the ASR step use the system Python.** `pip install whisperx` on 3.14
> does **not** error — pip and uv silently back-solve to whisperx 3.7.2 with
> torch 2.13 and transformers 5.15, which imports fine and fails at runtime.
> Always pin the version explicitly so a bad resolution fails loudly.

```powershell
uv venv --python 3.12 <repo>\.venv-asr

uv pip install --python <repo>\.venv-asr\Scripts\python.exe `
   whisperx==3.8.6 --torch-backend cu128
```

`--torch-backend cu128` pulls torch/torchaudio/torchvision from
`download.pytorch.org/whl/cu128` while everything else comes from PyPI. This
resolution was verified with `uv pip compile` (no install): torch 2.8.0+cu128,
ctranslate2 4.8.1, torchcodec 0.7.0.

> **UNVERIFIED:** the install itself has not been run on this machine. Only the
> dependency resolution was checked. Expect roughly 4 GB of wheels plus ~3.1 GB of
> Whisper weights plus ~360 MB for the German wav2vec2 bundle, from three separate
> caches (uv, HuggingFace, torch hub). Pass `--model_dir` and set `HF_HOME` /
> `TORCH_HOME` into one project-controlled directory so the cache is one thing you
> can point at, size and clear.

### Two version pins that are not negotiable

- **cu128, not cu130.** ctranslate2 4.x needs `cublas64_12.dll` specifically; a
  cu130 torch build ships `cublas64_13.dll`. The driver supports CUDA 13, so this
  looks like a perfectly valid choice right up to the crash.
- **ctranslate2 ≥ 4.6.** Below that it wants cuDNN **8** (`cudnn_ops_infer64_8.dll`),
  which modern torch wheels no longer ship. This is the trap in most 2023–2024
  blog posts; do not copy `pip install ctranslate2==4.4.0` from anywhere.

---

## <a id="cudnn"></a>The cuDNN failure, and why it happens

**Symptom:** the import succeeds, the model loads, and the first GPU inference dies
with `Could not locate cudnn_ops64_9.dll` or `cublas64_12.dll`.

**Cause**, established by reading the wheels' PE import tables rather than by
guessing: the ctranslate2 4.8.1 Windows wheel bundles only the thin
`cudnn64_9.dll` loader stub. At runtime that stub `LoadLibrary`s four more DLLs —
`cudnn_ops64_9.dll`, `cudnn_cnn64_9.dll`, `cudnn_adv64_9.dll`,
`cudnn_graph64_9.dll` — and `ctranslate2.dll` separately needs `cublas64_12.dll`.
**None of them are in the wheel**, and ctranslate2's `__init__.py` adds only its
own package directory to the DLL search path.

**Fix:** the torch 2.8.0+cu128 wheel already contains every one of those DLLs in
`torch/lib`. Put that directory on the DLL search path *before* importing
anything.

```python
# MUST run before `import whisperx` / `import ctranslate2`
import os, sys, pathlib
torch_lib = pathlib.Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
os.add_dll_directory(str(torch_lib))
os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ["PATH"]
import whisperx
```

No separate cuDNN download. No CUDA Toolkit. **UNVERIFIED** — mechanically derived
and highly likely, but not executed here.

Bake it into the wrapper script. A bare `whisperx.exe` from a fresh shell will
fail.

**Preflight it in two seconds** before burning time on a long file:

```powershell
.\.venv-asr\Scripts\python.exe -c "import os,sys,pathlib; p=pathlib.Path(sys.prefix)/'Lib/site-packages/torch/lib'; os.add_dll_directory(str(p)); os.environ['PATH']=str(p)+os.pathsep+os.environ['PATH']; import ctranslate2 as c; print(c.__version__, c.get_cuda_device_count(), c.get_supported_compute_types('cuda'))"
```

Expect a device count ≥ 1 and a list containing `float16` / `int8_float16`. If the
count is 0, stop — do not start a transcription.

---

## The three transcription tiers

The pipeline degrades rather than failing, and records **which** tier produced the
timestamps in `transcript.timestamp_source`. Downstream code reads that field to
decide how much to trust a cut point.

### Tier 1 — WhisperX + wav2vec2 forced alignment (default)

`timestamp_source: "wav2vec2-forced-align"`. True word boundaries measured
against the audio. This is what the cut algorithm's padding values assume.

```powershell
.\.venv-asr\Scripts\whisperx.exe "audio.wav" `
  --model large-v3 --language de --device cuda --compute_type float16 `
  --batch_size 8 --output_format json --output_dir .\transcript `
  --suppress_numerals --print_progress True
```

German picks `VOXPOPULI_ASR_BASE_10K_DE` automatically — no HuggingFace token.
Spell it out with `--align_model` only if you want the pin visible in
`project.json`.

`--model large-v3` is not optional: the default is `small`, which is noticeably
weak on German compound nouns. large-v3 at float16 is ~3.1 GB VRAM plus ~0.4 GB
for the alignment model — comfortable in 12 GB.

### Tier 2 — faster-whisper on CPU, same venv

`timestamp_source: "dtw"`. Word timestamps come from cross-attention dynamic time
warping, not forced alignment. Usable, less precise at boundaries. This is the
path when CUDA is unavailable.

### Tier 3 — ffmpeg's own `whisper` filter, zero install

`timestamp_source: "ffmpeg-whisper-token"`. **VERIFIED working on this machine
right now** with German audio; needs only a `ggml` model file.

```
ffmpeg -hide_banner -y -i audio.wav -vn -af "aresample=16000,whisper=model=ggml-base.bin:language=de:format=json:destination=words.json:max_len=1:queue=10" -f null -
```

```powershell
curl -sL -o ggml-base.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
```

**This is a rescue path, not a fallback you ship.** It emits BPE **tokens**, not
words, with integer-millisecond timestamps, and it duplicates and overlaps them at
10-second chunk boundaries. Fed straight into the cut algorithm it produces
garbage cut points. Re-join tokens into words on the leading-space convention,
drop any token whose `start` precedes the previous token's `start` (that is the
chunk-restart marker), and divide by 1000 before writing into `project.json`.

---

## Prepare the audio the same way for all three

```
ffmpeg -y -i input.mp4 -vn -map 0:a:0 -ac 1 -ar 16000 -c:a pcm_s16le -f wav whisper_in.wav
```

VERIFIED: 0.08 s for a 60 s file. Extracting up front means every tier reads
identical audio, so a tier switch changes only the model, not the input.

---

## Four things that will bite

**`--suppress_numerals` is mandatory, and it has a cost.** wav2vec2's character
dictionary has no digits, so numerals, currency and `%` come back with **no**
`start`/`end`, or with interpolated (fake) timestamps. A cut algorithm keying on
`word.start` will `KeyError`. Suppressing numerals makes Whisper spell them out,
which fixes alignment — but the transcript then says `zwanzig`, not `20`, and
burned captions will spell out every number. The escape hatch is
`captions.overrides`:

```jsonc
"overrides": [{ "word_index": 1204, "text": "20" }]
```

Read timestamps defensively regardless (`w.get("start")`) and record which words
were interpolated.

**UTF-8, always.** `--output_format json` writes unindented JSON with
`ensure_ascii=False`, i.e. raw UTF-8 umlauts. Reading it with Windows' default
cp1252 raises `UnicodeDecodeError`. Always `open(path, encoding="utf-8")`.
`.claude/settings.json` sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` as belt
and braces.

**`import torchcodec` will fail, and that is fine.** The installed ffmpeg 8.1.1 is
a static build with no DLLs; torchcodec 0.7.0 only links FFmpeg shared majors 4–7.
pyannote prints a scary warning and catches the import in a bare `except`.
WhisperX always decodes via the ffmpeg CLI. Ignore it. Only install the BtbN
FFmpeg 7.1 **shared** build if you ever call torchcodec directly.

**`--diarize` needs a HuggingFace licence.** It defaults to a gated model and 401s
*after* the transcription has already run. For single-speaker talking head, omit
it — the JSON then has no `speaker` key at all, so do not write code that assumes
one.

---

## Do not install these

- **stable-ts in the same venv.** Its `hf` extra pins `transformers <= 4.47.1`
  while whisperx needs `>= 4.48.0`. Separate venv or not at all.
- **Anything into `assets/fonts/`.** That directory is fonts only — libass loads
  every file it finds there and errors on non-fonts.
- **Third-party packages into the main pipeline.** Ever.

---

## Cache and disk

The cache is global, content-addressed, and lives at `.cache/` in the repo root on
`C:` (~554 GB free). `G:` has 11.5 GB and must not be used for it.

Run the collector from day one, not later:

```powershell
.\tools\ve.ps1 cache stats
.\tools\ve.ps1 cache gc --max-gb 80 --keep-days 21 --dry-run
```

LRU by last use, never touching anything referenced by a current `project.json`.
