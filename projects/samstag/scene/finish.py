"""Mux the rendered scene against the original voice-over and deliver.

The voice-over arrives already mastered (measured: I = -13.4 LUFS, TP = -0.3 dBFS,
LRA 2.7), which is inside every platform's target. Re-running loudnorm over it
would buy nothing and cost a processing generation on material with almost no
dynamic range left, so this only takes 1 dB of true-peak headroom -- AAC can
overshoot the source peak, and -0.3 leaves no room for that.

    python projects/samstag/scene/finish.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
FF = shutil.which("ffmpeg") or "ffmpeg"
FP = shutil.which("ffprobe") or "ffprobe"

SILENT = os.path.join(PROJ, "out", "scene_silent.mp4")
VO = os.path.join(PROJ, "media", "vo.mp3")
FINAL = os.path.join(PROJ, "out", "normaler_samstag_short.mp4")

HEADROOM_DB = -1.0


def probe(path: str, stream: str, keys: str) -> dict:
    out = subprocess.run(
        [FP, "-v", "error", "-select_streams", stream,
         "-show_entries", f"stream={keys}", "-of", "default=nw=1"],
        stdout=subprocess.PIPE, text=True, check=False,
    )
    return dict(l.split("=", 1) for l in out.stdout.strip().splitlines() if "=" in l)


def main() -> int:
    for p in (SILENT, VO):
        if not os.path.isfile(p):
            print(f"missing: {p}", file=sys.stderr)
            return 2

    cmd = [
        FF, "-y", "-loglevel", "error",
        "-i", SILENT,
        "-i", VO,
        "-filter_complex", f"[1:a]volume={HEADROOM_DB}dB,aresample=48000[a]",
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
        # -shortest keeps a stray tail frame from padding the end with silence.
        "-shortest", "-movflags", "+faststart",
        FINAL,
    ]
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        return 2

    v = subprocess.run(
        [FP, "-v", "error", "-count_frames", "-select_streams", "v",
         "-show_entries", "stream=nb_read_frames,width,height,r_frame_rate",
         "-of", "default=nw=1", FINAL],
        stdout=subprocess.PIPE, text=True, check=False).stdout.strip()
    d = subprocess.run(
        [FP, "-v", "error", "-show_entries", "format=duration,size",
         "-of", "default=nw=1", FINAL],
        stdout=subprocess.PIPE, text=True, check=False).stdout.strip()

    print(FINAL)
    for line in (v + "\n" + d).splitlines():
        print("  " + line)

    # Independent loudness check on the delivered file, not on the source.
    eb = subprocess.run([FF, "-hide_banner", "-nostats", "-i", FINAL,
                         "-af", "ebur128=peak=true", "-f", "null", "-"],
                        stderr=subprocess.PIPE, text=True, check=False).stderr
    keep = False
    for line in eb.splitlines():
        if "Integrated loudness" in line:
            keep = True
        if keep and line.strip():
            print("  " + line.strip())
        if keep and "Peak:" in line:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
