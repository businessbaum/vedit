"""The audio-only lane: no camera source, the picture IS the graphic.

The rest of the pipeline assumes footage exists and overlays go on top of it.
A voice-over with no picture is a different shape: one HTML scene is the whole
frame, and it is rendered by sampling it once per output frame.

Everything here obeys the same invariant as the overlay lane -- **the scene is a
pure function of ``?t=<seconds>``**. No CSS animation, no requestAnimationFrame,
nothing reading the clock. Headless Chrome has no virtual clock, so anything
time-driven would be sampled at an arbitrary moment: the motion would judder and
the frame cache would be a lie (same key, different pixels).

Three steps, each usable on its own:

    ve scene new <slug> <audio>   transcribe -> words.json + a scene to edit
    ve scene render <slug>        scene.html -> frames -> silent mp4
    ve scene finish <slug>        mux against the voice-over -> the deliverable
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unicodedata

from . import ffmpeg as ff

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

# Chrome exits 0 and writes a plausible PNG when it renders its own error page,
# so a whole render can "succeed" as 1600 identical blank frames. Any sample
# whose frames nearly all match is that failure, not content.
MIN_UNIQUE_FRACTION = 0.25


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    found = shutil.which("chrome") or shutil.which("chromium")
    if found:
        return found
    raise RuntimeError("Chrome not found; install it or set CHROME explicitly")


# --------------------------------------------------------------------------
# transcription -- the no-install path
# --------------------------------------------------------------------------

def _norm(word: str) -> str:
    """Fold a word to its comparable core, keeping umlauts (Whisper writes them
    correctly and transliterating would break the match)."""
    w = unicodedata.normalize("NFC", word)
    return re.sub(r"[^\wäöüÄÖÜß]", "", w).lower()


def merge_tokens_to_words(tokens: list[dict], sentences: list[dict]) -> list[dict]:
    """Fold ffmpeg's BPE tokens into whole words, timed.

    The ``whisper`` filter emits sub-word pieces with no leading spaces --
    ``Man`` + ``che`` + ``Menschen`` -- so word boundaries cannot be recovered
    from the token stream alone. The sentence text is the ground truth for where
    words end; the token stream supplies the timings.
    """
    targets: list[tuple[str, int]] = []
    for si, s in enumerate(sentences):
        for w in s["text"].split():
            targets.append((w, si))

    words: list[dict] = []
    ti = 0
    for surface, si in targets:
        core = _norm(surface)
        acc, start, end, used = "", None, None, 0
        while ti < len(tokens):
            tok = tokens[ti]
            if start is None:
                start = tok["start"]
            acc += tok["text"]
            end = tok["end"]
            ti += 1
            used += 1
            probe = _norm(acc)
            if core and probe.startswith(core):
                break
            if used > 6:  # a runaway match would eat the rest of the stream
                break
        words.append({
            "word": surface,
            "start": round((start or 0) / 1000.0, 3),
            "end": round((end or 0) / 1000.0, 3),
            "sent": si,
        })
    return words


def _parse_srt(text: str) -> list[dict]:
    out: list[dict] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", lines[1])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        out.append({
            "start": g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0,
            "end": g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0,
            "text": " ".join(lines[2:]).strip(),
        })
    return out


def transcribe_audio(audio: str, model: str, work_dir: str,
                     language: str = "de") -> tuple[list[dict], list[dict]]:
    """Word-level timings without installing anything.

    Uses ffmpeg's built-in ``whisper`` filter, which is already present in the
    gyan full build. Coarser than WhisperX forced alignment -- these are token
    boundaries, not phonetic ones -- but it needs no venv, no CUDA DLLs and no
    HuggingFace token, and it is accurate enough to hang graphics off.

    Every path is passed RELATIVE to ``work_dir``. A Windows drive-letter colon
    inside a filter argument is parsed as an option separator and the graph fails
    with ``No option name near '/Users/...'``.
    """
    os.makedirs(work_dir, exist_ok=True)
    wav = os.path.join(work_dir, "_asr.wav")
    ff.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", audio,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav])

    rel_model = os.path.relpath(model, work_dir).replace("\\", "/")
    out: dict[str, str] = {}
    for fmt, name in (("srt", "_sent.srt"), ("json", "_tokens.jsonl")):
        extra = ":max_len=1" if fmt == "json" else ""
        ff.run([
            "ffmpeg", "-hide_banner", "-nostats", "-y", "-i", "_asr.wav",
            "-af", (f"whisper=model={rel_model}:language={language}"
                    f":format={fmt}:destination={name}:queue=10{extra}"),
            "-f", "null", "-",
        ], cwd=work_dir)
        with open(os.path.join(work_dir, name), "r", encoding="utf-8") as fh:
            out[fmt] = fh.read()

    sentences = _parse_srt(out["srt"])
    tokens = [json.loads(l) for l in out["json"].splitlines() if l.strip()]
    words = merge_tokens_to_words(tokens, sentences)
    return words, sentences


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _shoot(job: tuple) -> tuple[int, bool]:
    idx, t, png, url, w, h, profile, chrome = job
    if os.path.isfile(png) and os.path.getsize(png) > 0:
        return idx, True  # cache hit
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--force-device-scale-factor=1",
         "--disable-lcd-text", "--font-render-hinting=none",
         f"--window-size={w},{h}", f"--user-data-dir={profile}",
         f"--screenshot={png}", f"{url}?t={t:.4f}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, check=False,
    )
    return idx, os.path.isfile(png) and os.path.getsize(png) > 0


def render_frames(html: str, cache_root: str, *, fps: int = 30,
                  duration: float, width: int = 1080, height: int = 1920,
                  workers: int = 5, progress=print) -> str:
    """Sample the scene once per frame. Returns the frame directory.

    Frames are keyed by the hash of the scene file, so re-running after an edit
    re-shoots everything, while re-running unchanged is free.
    """
    chrome = find_chrome()
    with open(html, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:16]

    frames_dir = os.path.join(cache_root, "scene", digest)
    os.makedirs(frames_dir, exist_ok=True)
    prof_root = os.path.join(cache_root, "chrome-prof")

    # The file:// URL must be a native Windows path. A Git-Bash-style /c/... path
    # makes Chrome render its own error page for every input, silently.
    url = "file:///" + os.path.abspath(html).replace(os.sep, "/")

    n = int(round(duration * fps))
    jobs = [(i, i / fps, os.path.join(frames_dir, f"{i:05d}.png"), url,
             width, height, os.path.join(prof_root, f"w{i % workers}"), chrome)
            for i in range(n)]

    warm = sum(1 for j in jobs if os.path.isfile(j[2]) and os.path.getsize(j[2]) > 0)
    progress(f"scene {digest}: {n} frames @ {fps} fps ({warm} cached)")

    t0, done, failed = time.time(), 0, []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, ok in ex.map(_shoot, jobs):
            done += 1
            if not ok:
                failed.append(idx)
            if done % 200 == 0 or done == n:
                el = time.time() - t0
                rate = done / el if el else 0
                progress(f"  {done}/{n}  {el:6.1f}s  {rate:4.1f} fps"
                         f"  eta {((n - done) / rate) if rate else 0:5.1f}s")
    if failed:
        raise RuntimeError(f"{len(failed)} frames failed to render (first: {failed[:6]})")

    sample = [jobs[i][2] for i in range(0, n, max(1, n // 40))]
    digests = {hashlib.md5(open(p, "rb").read()).hexdigest() for p in sample}
    if len(digests) < max(2, int(len(sample) * MIN_UNIQUE_FRACTION)):
        raise RuntimeError(
            f"only {len(digests)} unique frames in a {len(sample)}-frame sample -- "
            f"Chrome most likely rendered an error page rather than the scene")
    progress(f"  sanity: {len(digests)} unique digests across {len(sample)} sampled frames")
    return frames_dir


def encode_frames(frames_dir: str, out_path: str, fps: int = 30, crf: int = 17) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ff.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
            "-i", os.path.join(frames_dir, "%05d.png"),
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path])
    return out_path


def finish(silent_video: str, voice: str, out_path: str, *,
           headroom_db: float = -1.0, bitrate: str = "256k") -> str:
    """Mux the rendered picture against the original voice-over.

    A voice-over that arrives already mastered needs no second loudness pass --
    that only costs a processing generation on material with no dynamic range
    left. It does need true-peak headroom, because AAC can overshoot the source
    peak, which is what ``headroom_db`` buys.
    """
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ff.run(["ffmpeg", "-y", "-loglevel", "error",
            "-i", silent_video, "-i", voice,
            "-filter_complex", f"[1:a]volume={headroom_db}dB,aresample=48000[a]",
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", bitrate,
            "-ar", "48000", "-ac", "2",
            # a stray tail frame would otherwise pad the end with silence
            "-shortest", "-movflags", "+faststart", out_path])
    return out_path


def measure_loudness(path: str) -> dict:
    """What the delivered file actually measures -- not what a filter predicted."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                        "-af", "ebur128=peak=true", "-f", "null", "-"],
                       stderr=subprocess.PIPE, text=True, check=False)
    out: dict[str, float] = {}
    for key, pat in (("I", r"I:\s*(-?[\d.]+) LUFS"),
                     ("LRA", r"LRA:\s*(-?[\d.]+) LU"),
                     ("peak", r"Peak:\s*(-?[\d.]+) dBFS")):
        m = re.findall(pat, p.stderr)
        if m:
            out[key] = float(m[-1])
    return out
