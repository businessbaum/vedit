# Fonts

Bundled so captions and graphics render identically regardless of what Windows
happens to have installed.

> **This directory contains fonts and nothing else.** libass loads **every file**
> in `fontsdir` — confirmed in the verbose log, all six `.ttf`s are opened to
> render one word — and a stray `README.md` produces
> `Error opening memory font 'README.md'`. Documentation about the fonts lives
> here, in `docs/`.

## The table

`nameID 1` read out of the actual files this pass. These are the exact strings an
ASS `Style:` line must carry.

| file | ASS `Fontname` | `Bold` | licence |
| --- | --- | --- | --- |
| `Inter-Regular.ttf` | `Inter` | `0` | SIL OFL 1.1 |
| `Inter-Bold.ttf` | `Inter` | `-1` | SIL OFL 1.1 |
| `Inter-SemiBold.ttf` | `Inter SemiBold` | `0` | SIL OFL 1.1 |
| `Inter-ExtraBold.ttf` | `Inter ExtraBold` | `0` | SIL OFL 1.1 |
| `Montserrat-Bold.ttf` | `Montserrat` | `-1` | SIL OFL 1.1 |
| `Montserrat-ExtraBold.ttf` | `Montserrat ExtraBold` | `0` | SIL OFL 1.1 |

`vedit.captions.check_font_table()` re-derives this from the files themselves, so
a font swap cannot rot the table unnoticed. It returns an empty list when
everything agrees — verified empty this pass.

## Two rules that are not obvious

**Static single-weight files only. No variable fonts.** ASS has no way to select a
variable font's weight axis. `InterVariable.ttf` has `nameID 1 = "Inter Variable"`
and an `fvar` table; even a correct family match gives you Regular plus faux-bold,
never real weight 800. Worse, it fails *silently* — a style naming it rendered
byte-identical to an Arial control.

**A weight that is already part of the family name must not also set `Bold`.**
`Fontname: Inter ExtraBold` with `Bold: -1` asks libass for a bold of a bold. No
such face exists, so it falls back to DirectWrite/Arial, exits 0, and logs
nothing.

## The only way to catch a silent fallback

Render one caption frame and hash it against an Arial control. Different hash
means the bundled font was used; **identical hash is the failure**. VERIFIED this
pass on a 1280x720 black frame:

| style | md5 |
| --- | --- |
| `Fontname: Arial`, `Bold: -1` (control) | `8FF29A31D0F4A725E20044F545C1E7AA` |
| `Fontname: Inter ExtraBold`, `Bold: 0` | `9A811E44F27B61745BEF326C774E3D0F` |

This is QC check (e). Run it with `ve qc <slug> --check e`.

## Widths

German compound nouns are single unsplittable tokens, so caption grouping uses a
pixel budget rather than a character cap.
`estimate_text_width_px()` reads `hmtx`/`cmap` out of the real TTF, and agrees
with a libass render to within 0.5 %:

| `Geschwindigkeitsbegrenzung` @ 90 px | width | share of 1920 |
| --- | --- | --- |
| Montserrat ExtraBold | 924 px (rendered: 919 px) | 48 % |
| Inter ExtraBold | 1112 px | 58 % |

## Referencing a font

**In ASS:** by `Fontname` from the table above, with `fontsdir` pointing here.
The path needs Windows escaping — `fontsdir=C\\:/Users/…`, doubled backslash and
forward slashes. See
[ffmpeg-cookbook.md](ffmpeg-cookbook.md#captions).

**In an HTML graphic:** `@font-face` with a `file:///` URL, or a family that is
definitely installed. A missing font falls back silently there too, with no error
and no log.

## Adding a font

Drop the `.ttf`/`.otf` in, then run `check_font_table()` and add the row here with
the `nameID 1` string it reports. Only add fonts you are licensed to embed — OFL
and Apache-2.0 are safe, most foundry fonts are not.
