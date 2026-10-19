---
name: gfx-html
description: This skill should be used when the user asks for an on-screen graphic, title or overlay - "bau eine Bauchbinde", "mach ein Lower Third", "blend meinen Namen ein", "mach eine Titelkarte", "setz einen Text drüber", "brauch eine Grafik dazu", "zeig da eine Zahl an", "mach ein Intro-Insert", "build a lower third", "add a title card", "put a graphic over this", "overlay some text", "animate this number". Covers the HTML template contract, Chrome headless frame rendering, alpha-preserving movie encoding, and the overlays block in project.json.
argument-hint: [project-slug] [what-to-build]
---

# gfx-html

Graphics are HTML pages rendered one frame at a time by headless Chrome and encoded into an
alpha movie. Full request text is in `$ARGUMENTS`; the first token is usually the project
slug. Verb names follow the build contract; `python engine/ve.py --help` wins on conflict.

## The rule that makes this lane work

**Every graphic is a pure function of `?t=<seconds>`.** No CSS animations, no GSAP timelines,
no `requestAnimationFrame`, no transitions. The page reads `t` from `location.search`,
computes its layout, and renders one static frame.

This is not a style preference. `chrome --headless --screenshot` fires "when the page looks
loaded" - there is no virtual clock in this build, no `Emulation.setVirtualTimePolicy`, no
`beginFrame`. A CSS-animated page sampled that way returns a different pixel each run. That
destroys content-addressed caching (same key, different pixels) and produces visible judder.

A pure function of `t` sampled by the same naive loop is deterministic and cacheable, and
that is the whole difference.

## The budget, stated up front

Chrome renders at **~430-470 ms per frame** steady state, with a 7.9 s cold start. A 5-second
graphic at 30 fps is 150 frames, roughly **65 seconds single-threaded**. The engine
parallelises four Chrome processes and caches per-frame PNGs, so an unchanged graphic
re-renders for free - but a first render is not instant.

Tell the user the frame count and the estimate before starting a long one. Prefer short
graphics: a lower third that holds for 6 seconds does not need 6 seconds of motion, it needs
0.4 s in, a hold, and 0.4 s out.

## Step 1 - pick or write a template

Templates live in `presets/graphics/*.html`. Reuse before writing. A template is a single
self-contained HTML file that:

- Reads `t` (seconds) and its props from `location.search`.
- Computes all positions/opacities from `t` with plain arithmetic. Ease functions are fine -
  they are pure.
- Renders on a transparent body. Never paint a background colour.
- Uses fonts from `assets/fonts/` via `@font-face` with a `file:///` URL. Do not rely on
  system fonts - what Chrome resolves on this box is not what the QC control render assumes.
- Is legible at 1920x1080 and inside the platform's caption safe area. Check
  `output.platform`: a lower third that sits in the bottom third is under the TikTok UI.

Layout details, easing conventions and the props contract are in `docs/graphics.md`. Do not
restate them here.

## Step 2 - render frames

```
python engine/ve.py gfx <slug>
```

Per frame the engine invokes Chrome as an argv list, roughly:

```
chrome.exe --headless=new --disable-gpu --no-sandbox --hide-scrollbars
           --user-data-dir=<win-path>\prof --default-background-color=00000000
           --window-size=1920,1080 --screenshot=<win-path>\frame_0007.png
           "file:///C:/.../template.html?t=0.233&title=..."
```

Two flags carry the whole lane:

- `--default-background-color=00000000` is what produces genuine alpha. Verified: the corner
  pixel reads `0 0 0 0`. Without it you get opaque black and the overlay covers the video.
- The `file:///` URL must be a real Windows path. A Git Bash `$PWD` path fails **silently** -
  three different HTML files produced the identical md5 of a blank dark-mode error page, exit
  0, plausible file size. This is why QC check (g) compares against the known-blank hash.

Frames are cached under `.cache/html/` keyed on
`sha256(html_content + t + params + renderer + renderer_version)`. Changing the template
invalidates every frame, which is correct.

## Step 3 - encode with alpha intact

Same command. `ve gfx` renders the frames and encodes them in one pass - there is
no separate encode verb, because a PNG sequence with no movie next to it is a
half-finished artifact nothing downstream can use.

PNG sequence to **QuickTime RLE (`qtrle`, `argb`)**. ProRes 4444 and a raw PNG sequence also
round-trip alpha correctly. **VP9-in-WebM silently discards alpha in this build**, even with
`-metadata alpha_mode=1` - do not use it, and do not accept it from a user's asset either
without probing.

Output goes to `.cache/ovl/<xx>/<key>.mov` with a `<key>.json` sidecar.

## Step 4 - anchor the overlay

Write the `overlays[]` entry: `id`, `kind: "html"`, `template`, `props`, `anchor`,
`duration_frames`, `layer`, `render`.

**`anchor` is always `{segment_id, offset_frames}` - never an absolute timeline position.**
Timeline positions are derived from a running sum over enabled segments. The moment anyone
disables a segment upstream, every absolute position is wrong and every graphic drifts. An
anchored overlay moves with its segment automatically, which is the behaviour an editor
expects.

`layer` orders compositing; higher is on top. Captions burn last and sit above graphics.

## Step 5 - verify before declaring it done

Three checks, all cheap:

1. **Alpha probe.** Read one raw rgba pixel from a corner frame and assert `a = 0`. QC (d).
2. **Blank-page check.** Assert the frames are not each one flat colour. QC (g). This catches
   a bad `file:///` URL, a font that failed to load, and a JS error, all of which otherwise
   exit 0 with a plausible file. It samples the first, middle and three-quarter frames and
   fails only if *all* of them are uniform - every template here opens at opacity 0, so
   frame 0 of a working graphic is legitimately blank and judging on it alone fails a correct
   render.

`ve gfx` runs both automatically over what it just made and exits 2 if either fails.
3. **Determinism.** Render one frame twice and compare hashes. If they differ, something in
   the template is not a pure function of `t` - find it. This check exists because the
   failure it catches is invisible until a cached render disagrees with a fresh one.

Then show the user a still at the midpoint. Do not describe a graphic in prose when a
screenshot is one command away.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Overlay covers the video with black | `--default-background-color` missing, or the template paints a background | Add the flag; make the body transparent |
| Every frame identical, plausible file size | Blank error page from a bad `file:///` URL | Use a real Windows path; assert against the blank hash |
| Same key, different pixels between runs | Template is not a pure function of `t` | Remove all CSS animation and rAF |
| Text renders in the wrong typeface | System font fallback | `@font-face` from `assets/fonts/` with a `file:///` URL |
| Alpha lost after encoding | VP9/WebM | Use `qtrle` argb or ProRes 4444 |
| Graphic drifts after an upstream edit | Absolute timeline anchor | Anchor to `{segment_id, offset_frames}` |
| Render takes minutes | 430 ms/frame is the floor | Shorten the graphic or accept it; the cache makes the second run free |
