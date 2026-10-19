# Graphics — HTML rendered as a function of `t`

## The one rule

**Every graphic is a pure function of `?t=<seconds>`.**

No `@keyframes`. No CSS transitions. No GSAP. No `requestAnimationFrame`. No
`setTimeout`. The page reads `t` out of the query string, computes its layout for
that instant, writes it to inline styles, and stops.

This is not a style preference. Headless Chrome's `--screenshot` fires "when the
page looks loaded" — there is **no virtual clock**, no
`Emulation.setVirtualTimePolicy`, no `beginFrame`. Anything that animates over
wall-clock time is sampled at an arbitrary moment. That means:

- the same URL produces different pixels on different runs, which makes
  content-addressed caching a lie, and
- consecutive frames land at unpredictable offsets, which is judder.

Making `t` the only clock turns the naive screenshot loop into a deterministic
renderer. VERIFIED this pass: the same URL screenshotted twice produced
**byte-identical PNGs** (md5 `FDC80D9B0E1D7A38290810FF88945C59` both times).

---

## The template contract

`vedit.gfx.frame_params()` builds the query string. A template may read any of
these and nothing else:

| param | meaning |
| --- | --- |
| `t` | seconds into the graphic, `"%.6f"` — **the only clock** |
| `d` | total duration in seconds, `"%.6f"` |
| `f` | frame index (integer) |
| `n` | total frames (integer) |
| `w`, `h` | window size in pixels |
| `p` | the props object as compact JSON, `sort_keys=True` |

So the boilerplate at the top of every template is the same four lines:

```js
var q = new URLSearchParams(location.search);
var t = parseFloat(q.get("t") || "0");
var d = parseFloat(q.get("d") || "0");
var props = {}; try { props = JSON.parse(q.get("p") || "{}"); } catch (e) {}
```

That kernel is **deliberately duplicated** in each template rather than shared. A
template is meant to be copied to make a new one, and a graphic that carries its
own clock cannot be broken by an edit to somebody else's file.

`presets/graphics/_base.css` carries the shared type scale, safe-area variables
(`--safe-x`, `--safe-y`, `--u`) and colour tokens. Link it; do not restate it.

---

## The render loop

```python
from vedit.gfx import render_html_frames, frames_to_alpha_movie

pngs = render_html_frames(template, props, duration_frames, tb, cache,
                          size=(1920, 1080), workers=4)
mov  = frames_to_alpha_movie(png_dir, out_path, tb)
```

Each frame is keyed on the template fingerprint, the props, the size, the frame
index, the timebase and the Chrome version — so an unchanged graphic re-renders
for free, and a changed template invalidates only itself.

### The Chrome invocation — VERIFIED

```
chrome.exe --headless=new --disable-gpu --no-sandbox --hide-scrollbars
           --default-background-color=00000000
           --window-size=1920,1080
           --user-data-dir=<abs path>\prof
           --screenshot=<abs path>\out.png
           "file:///C:/…/stat_card.html?t=0.166667&d=3.000000&f=5&n=90&w=1920&h=1080&p=%7B…%7D"
```

| flag | why |
| --- | --- |
| `--headless=new` | the old headless mode is gone |
| `--default-background-color=00000000` | **genuine alpha.** Verified: a background pixel reads `0,0,0,0` |
| `--user-data-dir` | absolute Windows path, one per worker — concurrent Chromes sharing a profile fight over it |
| `--window-size` | the output resolution; lay the page out for exactly this |
| `--hide-scrollbars` | otherwise a grey strip appears down the right edge |

**Measured this pass:** 10 sequential frames of 1920x1080 in **3981 ms =
398 ms/frame**; a cold first frame ~720 ms.

That is the budget to plan against: a 6-second graphic at 30 fps is 180 frames ≈
72 seconds single-threaded. `render_html_frames` therefore defaults to
`workers=4` with one profile directory each.

> **UNVERIFIED:** the parallel throughput. Only sequential rendering was timed
> here.

### Encode the sequence — VERIFIED

```
ffmpeg -y -framerate 30 -i frames\f%04d.png -c:v qtrle -pix_fmt argb overlay.mov
```

Measured this pass: → `qtrle, 1920, 1080, argb, 10 frames`, and a transparent
pixel read back as `0,0,0,0` after the round trip.

`qtrle` is the default carrier: lossless, carries alpha, roughly 13× smaller than
ProRes 4444 on the flat graphics HTML produces. ProRes 4444
(`prores_ks -profile:v 4444 -pix_fmt yuva444p10le`) is the alternative.
**VP9-in-WebM does not carry alpha in this build** — it probes as `yuv420p` and
every pixel reads `a=255`, even with `-metadata alpha_mode=1`.

---

## A complete template

`presets/graphics/lower_third.html` is the reference implementation. Below is a
smaller one, written and rendered this pass, annotated for the reasons rather
than the syntax.

```html
<!--
  stat_card.html -- a number that counts up, centred, alpha over footage.
  PURE FUNCTION OF ?t=<seconds>. No @keyframes, no transition, no rAF.
  props: { label, value, suffix }
-->
<title>Stat Card</title>
<style>
  /* Transparent all the way down. If html or body paints a background,
     --default-background-color never reaches the output and every pixel
     comes back a=255. */
  html, body { margin: 0; padding: 0; background: transparent; }

  /* Lay out for the exact --window-size. Fixed px, not responsive units:
     the frame size is known, and % of an assumed viewport is a source of
     one-pixel differences between runs. */
  body { width: 1920px; height: 1080px; overflow: hidden;
         font-family: "Inter", system-ui, sans-serif; color: #f6f8fb; }

  .stage { position: absolute; inset: 0;
           display: flex; align-items: center; justify-content: center; }
  .value { font-size: 220px; font-weight: 800; line-height: 1;
           letter-spacing: -0.03em;
           /* tabular-nums: without it the counter jitters horizontally
              as digits change width, which reads as a rendering bug. */
           font-variant-numeric: tabular-nums; }
  .label { font-size: 44px; font-weight: 500; opacity: .72; margin-top: 18px; }
</style>

<div class="stage">
  <div class="card" id="card">
    <div class="value" id="value"></div>
    <div class="label" id="label"></div>
  </div>
</div>

<script>
(function () {
  // Every input arrives in the query string. No fetch, no external asset:
  // a network request would make the render depend on something the cache
  // key cannot see.
  var q = new URLSearchParams(location.search);
  var t = parseFloat(q.get("t") || "0");
  var d = parseFloat(q.get("d") || "0");
  var props = {};
  try { props = JSON.parse(q.get("p") || "{}"); } catch (e) { props = {}; }

  var IN = 0.6, OUT = 0.35;
  var clamp    = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };
  var outCubic = function (p) { return 1 - Math.pow(1 - clamp(p), 3); };

  // The animation IS this arithmetic. There is no timeline object and no
  // clock, so frame 7 renders identically whether it is computed first,
  // last, on another core, or a week later.
  var enter = outCubic(t / IN);
  var exit  = d > 0 ? outCubic((d - t) / OUT) : 1;
  var k     = Math.min(enter, exit);

  // The count-up is a function of t as well -- never a counter that
  // increments per call.
  var target = Number(props.value || 0);
  document.getElementById("value").textContent =
      Math.round(target * enter) + (props.suffix || "");
  document.getElementById("label").textContent = props.label || "";

  // toFixed() on every computed number. Floating point that stringifies as
  // 0.9199999999999999 on one run and 0.92 on another is a cache miss that
  // looks like a rendering bug.
  var card = document.getElementById("card");
  card.style.opacity   = k.toFixed(4);
  card.style.transform = "scale(" + (0.92 + 0.08 * k).toFixed(4) + ")";
})();
</script>
```

Rendered through the real `frame_params()` / `file_url()` / `chrome_screenshot()`
path this pass, with `props = {"label": "schneller als Echtzeit", "value": 42,
"suffix": "x"}`:

| `f` | `t` | bytes | md5 |
| --- | --- | --- | --- |
| 0 | 0.000000 | 10532 | `9e0a3e8b…` (fully transparent — `k = 0`) |
| 5 | 0.166667 | 29413 | `2831088d…` |
| 18 | 0.600000 | 26061 | `7bc4caf7…` |
| 60 | 2.000000 | 26061 | `7bc4caf7…` (settled — identical by design) |

`t` genuinely reaches the page; frames 18 and 60 hash identically because the
graphic has finished animating by then, which is the correct behaviour and also
a free cache hit.

### Writing your own

- **Fonts.** Reference `assets/fonts/` with `@font-face` and a `file:///` URL, or
  name a family that is definitely installed. A missing font falls back silently
  and the graphic renders in the wrong face with no error.
- **No `Date`, no `Math.random()`, no `performance.now()`.** Anything that is not
  a function of `t` and the props breaks determinism by construction.
- **Keep it self-contained** apart from `_base.css`. An external image is another
  thing that can fail to load in the 400 ms before the screenshot fires.
- **Give long German strings room.** `max-width: 26ch` on a title and a dedicated
  fade zone at the end of a plate are how `lower_third.html` avoids eating the
  last characters of a compound noun.

---

## Where a graphic sits on the timeline

```jsonc
{
  "id": "ovl_1", "kind": "html",
  "template": "presets/graphics/lower_third.html",
  "props": { "title": "Frame-Snapping", "subtitle": "warum Ganzzahlen gewinnen" },
  "anchor": { "segment_id": "seg_0003", "offset_frames": 12 },
  "duration_frames": 180, "layer": 10,
  "render": { "cache_key": "7d21ab…", "path": "../../.cache/ovl/7d/7d21ab….mov" }
}
```

Anchored to `{segment_id, offset_frames}`, **never** to an absolute timeline
frame. Re-cut anything upstream and the graphic follows the shot it belongs to. An
absolute position would silently drift onto the wrong sentence.

Composite it — VERIFIED:

```
[1:v]format=rgba,setpts=PTS-STARTPTS+<tl_in_seconds>/TB[gfx];
[0:v][gfx]overlay=x=0:y=0:eof_action=pass:format=auto[v]
```

`eof_action=pass` keeps the footage running after the graphic ends. `layer`
orders stacked overlays.

---

## The three ways this fails silently

### 1. The blank page — VERIFIED this pass

Chrome renders its own error page, screenshots **that**, and exits **0** with a
plausible file size. Two different missing URLs both produced:

```
exit=0   size=18097   md5=64246983D924E149DACCA9B3FFC5CC24
```

The same hash the research recorded earlier — it is stable enough to use as a
constant. Nothing in the exit code, the file size or stderr tells you the graphic
did not render.

Causes: a wrong path; a POSIX-style `file://` URL built from a Git Bash `$PWD`
(`cygpath -m` fixes that one); a template that threw before painting.

`gfx.chrome_screenshot()` refuses non-native paths and compares every output
against the known-blank hash for exactly this reason. **QC check (g)** repeats it
on the finished overlay.

### 2. Alpha that is not there

`--default-background-color=00000000` only works if nothing paints over it. A
`body { background: #fff }` left in a template, or an encode to a carrier that
drops alpha, yields an opaque graphic that looks fine as a PNG and covers the
whole frame when composited.

**QC check (d)** dumps one raw rgba pixel and asserts `a=0`:

```
ffmpeg -y -i overlay.mov -vf "format=rgba,crop=1:1:10:10" -frames:v 1 -f rawvideo -pix_fmt rgba probe.raw
```

It also rejects the pixel format on the codec alone — a transparent-looking
preview in a player that composites over its own background is the classic false
pass.

### 3. A cache key that does not cover everything

If the template file changes but its bytes are not in the key, a stale render is
served forever and the change appears to have no effect. The key includes the
template **fingerprint** (not its path), the props, the frame index, the size, the
timebase and the Chrome version:

```jsonc
{ "v": 1, "renderer": "chrome-headless", "renderer_version": "…",
  "fingerprint": "…", "props": { … }, "frame": 7,
  "timebase": [30, 1], "size": [1920, 1080] }
```

`renderer` and `renderer_version` are in the key so a different renderer can be
slotted in later without silently reusing another one's frames.

---

## What was deliberately not used

A third-party HTML render service was evaluated and rejected for v1: no local
partial render (which kills the per-segment cache that is the point of this
design), a self-updating engine (which makes content-addressed caching a lie), and
an `init` command that deletes existing agent skill directories. The adapter seam
— `renderer` and `renderer_version` in the cache key — exists so that decision can
be revisited without a rewrite.

If a graphic ever genuinely needs a virtual clock — third-party animated content
that cannot be rewritten as a function of `t` — the escalation is a raw CDP
WebSocket driver (~150 lines, stdlib only) using `Emulation.setVirtualTimePolicy`.
Not before.
