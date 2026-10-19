---
name: transcribe
description: This skill should be used when the user asks for a word-level transcript of a source - "transkribier das", "mach das Transkript", "schreib mit was ich sage", "erstell das Transkript", "was habe ich gesagt", "lass WhisperX laufen", "brauch die Wörter mit Zeitstempeln", "transcribe this", "run the transcription", "get me the transcript", "word-level timestamps", "run WhisperX". Covers the isolated ASR venv, the Windows cuDNN PATH fix, German alignment, and writing the transcript block in project.json. Does not burn captions - that is the captions skill.
argument-hint: [project-slug] [source-id]
arguments: [project, sourceid]
---

# transcribe

Produce word-level timestamps for one source and register them in `project.json`. Everything
downstream - story blocks, cuts, captions - anchors on word indices from this file, so a bad
transcript is not a small problem.

Arguments: `$project` slug, `$sourceid` (default `A`, the `acam`). Full request in
`$ARGUMENTS`. An empty `$`-placeholder means the skill auto-triggered rather than being
slash-invoked: take the project from `PIPELINE_STATE` in the session context and say which
one you picked. Verb names follow the build contract; `python engine/ve.py --help` wins on
conflict.

## The constraint that shapes this stage

**WhisperX cannot run on the main pipeline's Python.** WhisperX 3.8.6 declares
`requires-python <3.14` and pins `torch~=2.8.0`, which has no cp314 wheels. The system Python
is 3.14.2 and is the only one installed. So ASR lives in an isolated venv and is reached
**only** through `subprocess`.

Never `pip install` anything into the system Python to make this work. The main pipeline is
stdlib-only and stays that way.

## Step 1 - ensure the ASR sidecar exists

```
uv venv --python 3.12 .venv-asr
uv pip install --python .venv-asr whisperx==3.8.6 --torch-backend cu128
```

CPython 3.12.12 is already on disk, so this downloads no interpreter. The resolve is known
good: torch 2.8.0+cu128, ctranslate2 4.8.1, torchcodec 0.7.0.

Installing packages is an `ask` permission - expect a prompt and let the user approve it.

## Step 2 - the cuDNN PATH fix, before the first run

This is the single failure that costs 40 minutes if unaddressed. The ctranslate2 Windows
wheel bundles only the thin `cudnn64_9.dll` loader stub. At runtime it `LoadLibrary`s
`cudnn_ops64_9.dll`, `cudnn_cnn64_9.dll`, `cudnn_adv64_9.dll` and `cudnn_graph64_9.dll`, and
`ctranslate2.dll` separately needs `cublas64_12.dll`. None of those are in the wheel, and
ctranslate2 adds only its own package directory to the DLL search path.

The torch cu128 wheel ships every one of them. So:

```
PATH = <repo>\.venv-asr\Lib\site-packages\torch\lib  +  existing PATH
```

That is the entire fix. No separate cuDNN download, no CUDA Toolkit.

Preflight it in two seconds rather than discovering it mid-run:

```
.venv-asr\Scripts\python.exe -c "import ctranslate2; print(ctranslate2.get_cuda_device_count())"
```

Expect `1` on the RTX 4070 SUPER. A `0` or an import error means the PATH prepend did not
take - fix that before transcribing, do not fall back silently.

## Step 3 - transcribe

```
python engine/ve.py transcribe $project --source $sourceid
```

The engine extracts audio, runs the sidecar, normalizes the output and commits it to
`.cache/transcript/<strong_key>.words.json`. The cache is keyed on the source's content
hash, so re-running on an unchanged source is free and re-transcribing a re-normalized file
is automatic.

Settings that matter and why:

- **Language `de`, explicitly.** Auto-detect on a German speaker who uses English technical
  terms will sometimes pick `en` and produce garbage.
- **Align model `VOXPOPULI_ASR_BASE_10K_DE`.** torchaudio picks it automatically for `de`,
  no HF token needed. If anything in the config names an English wav2vec2 model next to
  `"language": "de"`, that is a bug - fix it.
- **`--suppress_numerals` is mandatory.** Numerals are the unalignable-character class that
  makes `word.start` raise KeyError. Suppressing them costs you literal digits: the transcript
  says `zwanzig`, not `20`. That is a real cost and it surfaces in burned captions - the fix
  is a `captions.overrides` entry, not turning the flag off. Tell the user when numbers appear.

## Step 4 - degrade honestly, and record how

Three tiers, in order:

1. WhisperX with wav2vec2 forced alignment -> `timestamp_source: "wav2vec2-forced-align"`.
   True word boundaries. This is the only tier that supports accurate cutting.
2. Whisper DTW timestamps -> `"dtw"`. Approximate. Widen all padding; warn on cuts.
3. ffmpeg's built-in `whisper` filter -> `"ffmpeg-whisper-token"`. Zero install, confirmed
   working on this box with German audio, but it emits BPE-token timestamps, not word
   timestamps. Usable to read the content, **not** usable to cut on.

Always write the tier you actually got into `transcript.timestamp_source` and say it out
loud. A silent fallback to tier 3 produces cuts that land mid-word and nobody knows why.

## Step 5 - register and sanity-check

Write the `transcript` block: `source_id`, `path` (project-relative into `.cache/`),
`engine`, `align_model`, `language`, `timestamp_source`, `word_count`. Set `stage` to
`"transcribed"`.

Then check three things before handing off:

- **Coverage.** Last word `end` should land within a few seconds of the source duration. A
  transcript that stops at 40% means the audio stream was truncated or the wrong track was
  extracted.
- **Word rate.** `word_count` / minutes should be 130-160 for German talking head. Far below
  means dropped audio; far above means hallucinated repetition.
- **Low-confidence words.** Count words with `score < 0.35`. These are timestamp-unreliable:
  padding widens by 60 ms and cuts inside them are forbidden.

**Never treat `score` as a filler signal.** It is an alignment score. Low means "the audio
does not match these letters", which correlates with rare words and proper nouns - the exact
opposite of a filler. Using it to find `ähm` removes the speaker's most specific vocabulary.

## Step 6 - offer the next stage

Recommend cut-story, not cut-rough. Mechanical cutting on material that is about to be
dropped wholesale is wasted work, and the story pass is where the video is actually decided.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Could not locate cudnn_ops64_9.dll` | torch/lib not on PATH | Step 2; verify with the two-second preflight |
| `KeyError: 'start'` on a word | Numerals not suppressed | `--suppress_numerals`, then re-run |
| Transcript is English-ish nonsense | Language auto-detected | Force `--language de` |
| Timestamps drift later in the file | Fell back to tier 2 or 3 | Read `timestamp_source`; do not cut on tier 3 |
| Captions spell out numbers | `--suppress_numerals` working as designed | Add `captions.overrides` entries |
| Transcription reruns on an unchanged file | `strong_key` changed | The source was re-normalized; that is correct, not a bug |
