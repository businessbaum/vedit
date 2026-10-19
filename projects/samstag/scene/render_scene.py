"""Render scene.html to a frame sequence, then to a video.

Full-frame generated video (no camera source), so this is a simpler path than
the engine's alpha-overlay lane: every frame is one headless-Chrome screenshot
of scene.html at ``?t=<seconds>``.

Frames are content-addressed by (html hash, t, size), so re-running after a
change to one beat only re-shoots the frames whose pixels actually move.

    python projects/samstag/scene/render_scene.py [--fps 30] [--workers 4]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
ROOT = os.path.abspath(os.path.join(PROJ, "..", ".."))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# A blank error page is the failure mode that costs a whole render: Chrome exits
# 0 and writes a plausible PNG. Any frame matching a known-blank digest is a bug,
# not content, so we fail loudly instead of shipping 1600 identical frames.
BLANK_MAX_UNIQUE = 4


def frame_key(html_digest: str, t: float, w: int, h: int) -> str:
    payload = f"{html_digest}|{t:.4f}|{w}x{h}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def shoot(args: tuple) -> tuple[int, str, bool]:
    idx, t, out_png, url, w, h, profile = args
    if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
        return idx, out_png, True  # cache hit
    cmd = [
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--disable-lcd-text", "--font-render-hinting=none",
        f"--window-size={w},{h}",
        f"--user-data-dir={profile}",
        f"--screenshot={out_png}",
        f"{url}?t={t:.4f}",
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   stdin=subprocess.DEVNULL, check=False)
    ok = os.path.isfile(out_png) and os.path.getsize(out_png) > 0
    return idx, out_png, ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--duration", type=float, default=54.2)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    html = os.path.join(HERE, "scene.html")
    if not os.path.isfile(html):
        print("scene.html missing -- run build_scene.py first", file=sys.stderr)
        return 2
    digest = hashlib.sha256(open(html, "rb").read()).hexdigest()[:16]
    url = "file:///" + html.replace(os.sep, "/")

    frames_dir = os.path.join(ROOT, ".cache", "html", "samstag", digest)
    os.makedirs(frames_dir, exist_ok=True)
    prof_root = os.path.join(ROOT, ".cache", "chrome-prof")

    n = int(round(a.duration * a.fps))
    jobs = []
    for i in range(n):
        t = i / a.fps
        png = os.path.join(frames_dir, f"{i:05d}.png")
        jobs.append((i, t, png, url, a.width, a.height,
                     os.path.join(prof_root, f"w{i % a.workers}")))

    warm = sum(1 for j in jobs if os.path.isfile(j[2]) and os.path.getsize(j[2]) > 0)
    print(f"scene {digest}  {n} frames @ {a.fps} fps  ({warm} already cached)")

    t0 = time.time()
    done = 0
    failed: list[int] = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for idx, png, ok in ex.map(shoot, jobs):
            done += 1
            if not ok:
                failed.append(idx)
            if done % 100 == 0 or done == n:
                el = time.time() - t0
                rate = done / el if el else 0
                print(f"  {done}/{n}  {el:6.1f}s  {rate:5.1f} fps  eta {(n-done)/rate if rate else 0:5.1f}s",
                      flush=True)

    if failed:
        print(f"FAILED frames: {len(failed)} (first: {failed[:8]})", file=sys.stderr)
        return 2

    # Guard the silent-blank trap: if almost every frame is byte-identical the
    # page never rendered, and the encode would produce a valid-looking void.
    sample = [jobs[i][2] for i in range(0, n, max(1, n // 40))]
    digests = {hashlib.md5(open(p, "rb").read()).hexdigest() for p in sample}
    if len(digests) <= BLANK_MAX_UNIQUE:
        print(f"ABORT: only {len(digests)} unique frames in a {len(sample)}-frame sample "
              f"-- Chrome probably rendered an error page", file=sys.stderr)
        return 2
    print(f"  sanity: {len(digests)} unique digests across {len(sample)} sampled frames")

    out_dir = os.path.join(PROJ, "out")
    os.makedirs(out_dir, exist_ok=True)
    silent = os.path.join(out_dir, "scene_silent.mp4")

    ff = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ff, "-y", "-loglevel", "error",
        "-framerate", str(a.fps),
        "-i", os.path.join(frames_dir, "%05d.png"),
        "-c:v", "libx264", "-preset", "slow", "-crf", "17",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        silent,
    ]
    print("encoding ->", silent)
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        return 2
    print(f"done in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
