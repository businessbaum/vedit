# Animation toolkit — what to reach for, and why

Two jobs need different kit:

- **A — audio only.** There is no picture. You build one. (`projects/samstag` is a
  worked example.)
- **B — finished video.** The picture exists. You put things on top of it.

Both render through the same door, so both obey the same rule.

## The editorial rule: on-screen text must ADD, never repeat

**If the voice is already saying it, do not also write it on screen.** A card
that spells out the sentence being spoken wastes the only picture you have and
makes the video read as auto-generated.

Worked example from `projects/samstag`, which got this wrong the first time:

| | |
| --- | --- |
| ❌ **Wrong** | A card reading *"ABSTAND — Niemand will neben einem Blitzmagneten stehen"* while the narrator speaks that exact sentence. |
| ✅ **Right** | The same beat as a **text-free graphic**: a lit figure inside a dashed ring, everyone else backing out of it. Same meaning, no words. |
| ✅ **Right** | The opening card (*"ROY"*, *"Eine wahre Geschichte"*, *"Abschieben nach Asgard"*) — the voice does not supply the name, the framing or the punch-line setup, so the text carries its own weight. |
| ✅ **Right** | The strike log. It does not transcribe the narration; it **restructures** it into a countable form the voice cannot deliver. |

So, before every insert, ask: *is the voice already doing this?*

- **Yes** → text-free graphic, a meme or image, or nothing at all.
- **No** → text is fine. Names, numbers, structure (counters, lists), framing and
  punch-line setups are exactly what it is for.

Word-synchronous **captions are exempt** — they serve sound-off viewing, which is
a different job from illustration.

When a beat needs a picture and drawing one is overkill, an image from the web is
a legitimate option — but for anything monetised, memes are usually someone
else's copyrighted frame. CC0 sources (Pexels, Pixabay) or a drawn SVG carry no
such risk, which is why the three interstitials here ended up as inline SVG.

## The rule that decides which tools are usable

**A tool is usable here only if it can be asked for one specific moment.**

The renderer does not play anything. It asks for frame *n*, screenshots, asks for
frame *n+1*, screenshots. Nothing runs in real time. So:

| Pattern | Usable? | Why |
| --- | --- | --- |
| `f(t) -> layout` (what we do now) | ✅ | trivially seekable |
| GSAP `tl.pause(); tl.seek(t)` | ✅ | timelines are seekable by design |
| anime.js `anim.seek(ms)` | ✅ | same |
| Lottie `goToAndStop(frame, true)` | ⚠️ | seekable, but see the expression caveat below |
| CSS `@keyframes` / `animation:` | ❌ | sampled at whatever moment the screenshot fires |
| `requestAnimationFrame` loops | ❌ | same — headless Chrome has no virtual clock |
| Anything reading `Date.now()` | ❌ | different pixels for the same input |

Getting this wrong fails **quietly**: the render completes, the file is the right
size, and the motion judders because every frame was sampled at an arbitrary
point. It also makes the frame cache lie — same key, different pixels.

## Authoring the motion

### Now: hand-written `f(t)` — no dependency

What `projects/samstag/scene/scene.html` uses. Small easing helpers, everything
computed from `t`:

```js
function outCubic(p){ p = Math.min(1, Math.max(0, p)); return 1 - Math.pow(1-p, 3); }
function rise(start, dur){ return outCubic((T - start) / dur); }        // 0 -> 1
function win(a, b, fi, fo){ /* fade in, hold, fade out */ }
function spike(at, dur){ var p=(T-at)/dur; return (p<0||p>1)?0:Math.pow(1-p,2.2); }
```

Best for: counters, staged reveals, cross-fades, anything driven by a beat table.
Costs nothing, cannot drift, and is readable a month later.

### Upgrade path: GSAP with a paused timeline

When the choreography gets too fiddly to hand-roll (staggered lists, motion
paths, morphing), build a **paused** GSAP timeline and seek it:

```js
const tl = gsap.timeline({ paused: true });
tl.from('.row', { y: 40, opacity: 0, stagger: 0.08, ease: 'power3.out' }, 0.5);
tl.to('#count', { innerText: 7, snap: { innerText: 1 } }, '<');
tl.seek(T);            // T from ?t= — this line is the whole contract
```

This is the same mechanism HyperFrames uses, and it is the sanctioned way to get
rich authoring without giving up determinism. GSAP core is free; some plugins
are not — check before shipping.

### Pre-made animation: Lottie

After-Effects animations exported as JSON, played in the browser. Seek with
`anim.goToAndStop(frame, true)`.

**Caveat, documented upstream:** Lottie files that use *expressions* may not
render deterministically when seeked, which shows up as flicker. Remotion
documents the same limitation. Rule: use Lottie for simple icon and accent
animations, verify by rendering the same `t` twice and comparing hashes, and
keep expression-heavy files out.

## Case A — audio only, you build the picture

### 1. Kinetic typography driven by the transcript

The default, and usually the strongest. Word-level timings turn the narration
into the animation track: text lands on the word that says it. See
[graphics.md](graphics.md) and the `samstag` scene for the pattern.

### 2. Audio-reactive visuals — all verified present in this ffmpeg

| Filter | Gives you |
| --- | --- |
| `showwaves` | classic scrolling waveform |
| `showwavespic` | one static waveform image (good as a design element) |
| `showspectrum` | spectrogram |
| `showcqt` | musical-pitch spectrum, the "nice looking" one |
| `showfreqs` | frequency bars |
| `showvolume` | level meters |
| `avectorscope` | stereo field |

These react to the actual audio, so they are free motion that is always in sync.
Render one to an alpha-capable format and composite it under the type.

```
ffmpeg -i vo.mp3 -filter_complex "[0:a]showcqt=s=1080x400:count=2" -c:v qtrle waves.mov
```

### 3. Stock b-roll

Free, commercially usable, API-accessible:

| Source | License | API |
| --- | --- | --- |
| Pexels | Pexels License (commercial, no attribution) | free key, ~200 req/h |
| Pixabay | Pixabay License (CC0-like) | free key |
| Coverr / Mixkit / Videvo | mostly CC0 | mixed / scraping-unfriendly |

**The real risk is not the licence text — it is releases.** A free clip showing a
recognisable face, a logo, or private property rarely carries the model or
property release you would need for a paid campaign. For organic social posts
this is usually fine; for anything sponsored, check.

## Case B — finished video, you add things on top

The pipeline already has this lane: `overlays[]` in `project.json`, rendered to
an alpha movie and composited by `render`.

### Alpha carriers (measured on this machine)

| Codec | Alpha | Notes |
| --- | --- | --- |
| `qtrle` (`-pix_fmt argb`) | ✅ | lossless, **13× smaller** than ProRes for flat graphics — the default |
| `prores_ks -profile:v 4444` | ✅ | use for gradient-heavy work |
| VP9 / WebM | ❌ | alpha does **not** survive on this build — ruled out |

Always verify alpha survived rather than trusting exit codes:

```
ffmpeg -i graphic.mov -vf format=rgba -frames:v 1 -f rawvideo -pix_fmt rgba probe.raw
# a known-transparent pixel must read a=0
```

### Transitions — `xfade`, 45 of them, all present

`fade fadeblack fadewhite fadegrays dissolve distance pixelize radial hblur
wipe{left,right,up,down,tl,tr,bl,br} slide{left,right,up,down}
smooth{left,right,up,down} circle{open,close,crop} cover{left,right,up,down}
reveal{left,right} squeeze{h,v} zoomin rect{crop} diag{tl,tr,bl,br} hlwind`

`gltransition` (the GL-Transitions set) is **not** compiled into this build.

### Green screen

`chromakey` plus `despill` are both present — `despill` is the one people forget,
and it is what removes the green fringe that makes a key look cheap.

### Making an overlay stick to something that moves

Not solved here. Options, in increasing order of effort: anchor to a fixed screen
region (works for most talking-head lower thirds), hand-keyframe a few positions
and interpolate, or bring in optical-flow tracking. Only reach for the last one
when the shot genuinely demands it.

## Assets

| Kind | Source | Licence |
| --- | --- | --- |
| Fonts | bundled in `assets/fonts/` | SIL OFL — see [fonts.md](fonts.md) |
| Icons | Lucide (~1000 SVG) | MIT |
| Icons | Phosphor | MIT |
| Animated icons | lucide-animated, AnimateIcons | MIT |
| Lottie | LottieFiles free library | Lottie Simple License — commercial, no attribution |
| Emoji | system font, as used in `samstag` | fine for social; not a brand asset |

Prefer **SVG inlined into the HTML** over icon fonts: no fallback risk, and you
can drive stroke/fill from `t` like anything else.

## Safe areas for vertical

Platform UI eats the frame. Keep anything that must be read inside:

| Platform | Keep clear |
| --- | --- |
| TikTok | bottom ~18 %, right ~12 % |
| Reels / Shorts | bottom ~15 %, right ~10 % |
| all | top ~8 % |

`presets/platforms.json` carries these; the caption band in the `samstag` scene
sits at y 1390–1730 of 1920 for exactly this reason.

## Rendering cost, so you can budget

Measured on the real 54 s `samstag` render: **4.4 frames/s with 5 workers** at
1080x1920 — 1626 frames in 366 s. That is well short of the 5x you would predict
from the 430 ms single-frame figure, because this renderer starts a fresh Chrome
per frame and pays the process launch every time. Budget from the measured
number, not the theoretical one:

| Length | Frames @30 | 5 workers, measured |
| --- | --- | --- |
| 15 s | 450 | ~1.7 min |
| 54 s | 1620 | ~6.2 min |
| 3 min | 5400 | ~20 min |

The obvious win, if this ever gets annoying: keep worker Chromes alive and drive
them over CDP instead of relaunching per frame. Worth roughly a 3-4x speedup and
about 150 lines.

Frames are content-addressed, so changing one beat re-shoots only the frames
whose pixels move. That is what makes iteration cheap — the second render of a
tweaked scene is seconds, not minutes.

## What to avoid

- **CSS animations and `rAF`** — nondeterministic under screenshot capture.
- **Anything that self-updates at render time** (CDN scripts, remote fonts, live
  data). A renderer whose behaviour changes between two runs makes the cache a
  lie. Vendor everything locally.
- **Variable fonts in ASS subtitles** — libass cannot select a weight axis and
  falls back to Arial silently. See [fonts.md](fonts.md).
- **Lottie files with expressions** — see the caveat above.
