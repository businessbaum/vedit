"""Scene source for an audio-only project. Edit the BEATS table, rerun, render.

    python build_scene.py            # -> scene.html
    ve scene render <slug>           # does the above, then shoots every frame
    ve scene finish <slug>           # muxes it against the voice-over

THE ONE RULE: scene.html is a PURE FUNCTION of ``?t=<seconds>``. No CSS
animation, no requestAnimationFrame, nothing reading the clock. The renderer
does not play the scene -- it asks for one moment at a time and screenshots it.
Anything time-driven would be sampled at an arbitrary point, so the motion would
judder and the frame cache would be a lie.

THE EDITORIAL RULE: on-screen text must ADD, never repeat. If the voice is
already saying it, do not also write it -- use a text-free graphic, an image, or
nothing. Text earns its place for names, numbers, structure (counters, lists)
and punch-line setups. See docs/animation-toolkit.md.
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_fonts() -> str:
    """Walk up to the repo's assets/fonts. A fixed number of ``..`` hops only
    happens to be right at one depth, and this file is copied to another."""
    d = HERE
    for _ in range(6):
        candidate = os.path.join(d, "assets", "fonts")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise SystemExit("assets/fonts not found above " + HERE)


FONT_DIR = _find_fonts()

# ---------------------------------------------------------------------------
# EDIT THIS. Anchor every beat to the word that actually carries it -- read the
# times out of words.json rather than guessing from the sentence grid, or the
# graphic lands next to the line instead of on it.
# ---------------------------------------------------------------------------
BEATS = [
    {"id": "hook",  "in":  0.00, "out":  6.00, "kicker": "Kicker",
     "head": "TITEL", "lead": "Ein Satz, den die Stimme<br>so <b>nicht</b> sagt."},
    {"id": "mitte", "in":  6.00, "out": 20.00, "kicker": "Zwischenkapitel",
     "head": "", "lead": ""},
    {"id": "ende",  "in": 20.00, "out": 26.00, "kicker": "",
     "head": "Schluss.", "lead": ""},
]

# ---------------------------------------------------------------------------
# caption grouping -- a pixel-ish budget, never a character cap. German compound
# nouns are single unsplittable tokens that blow any max_chars.
# ---------------------------------------------------------------------------
WIDE = set("mwMW@")
NARROW = set("iljItfr.,!?:;'")


def width_units(text: str) -> float:
    return sum(1.35 if c in WIDE else 0.48 if c in NARROW else 1.0 for c in text)


def group_words(words, budget=21.0, max_words=5, max_dur=2.0):
    chunks, cur = [], []

    def flush():
        if cur:
            chunks.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                           "words": [{"w": w["word"], "s": w["start"], "e": w["end"]}
                                     for w in cur]})
            cur.clear()

    for w in words:
        trial = cur + [w]
        gap = (w["start"] - cur[-1]["end"]) if cur else 0.0
        if cur and (width_units(" ".join(x["word"] for x in trial)) > budget
                    or trial[-1]["end"] - trial[0]["start"] > max_dur
                    or len(trial) > max_words or gap > 0.45):
            flush()
        cur.append(w)
        if re.search(r"[.!?:]$", w["word"]):
            flush()
    flush()
    # Hold each chunk until the next begins; fading out at its own end leaves a
    # gap with no caption on screen, which reads as a blink several times a
    # sentence.
    for i, c in enumerate(chunks):
        c["until"] = chunks[i + 1]["start"] if i + 1 < len(chunks) else c["end"] + 0.70
    return chunks


def main() -> None:
    with open(os.path.join(HERE, "words.json"), "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    data = {
        "duration": float(doc.get("duration") or 0) + 0.4,
        "chunks": group_words(doc["words"]),
        "beats": BEATS,
    }
    html = TEMPLATE.replace("/*__DATA__*/ null", json.dumps(data, ensure_ascii=False))
    out = os.path.join(HERE, "scene.html")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print(f"{out}  ({len(html)} bytes, {len(data['chunks'])} caption chunks)")


TEMPLATE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Scene</title>
<style>
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-ExtraBold.ttf'); font-weight:800; }
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-Bold.ttf');      font-weight:700; }
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-SemiBold.ttf');  font-weight:600; }
  :root{ --ink:#FFF; --dim:#8A93A6; --accent:#FFE14D; --cold:#8FD8FF; --hot:#FF7A45; }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:1080px;height:1920px;overflow:hidden;background:#06070B}
  body{font-family:'X',system-ui,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}
  #sky{position:absolute;inset:0}
  #vig{position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(78% 58% at 50% 42%,rgba(0,0,0,0) 40%,rgba(0,0,0,.72) 100%)}
  .stage{position:absolute;left:70px;right:70px;top:205px;height:1130px}
  .layer{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
  .layer > div{width:100%}
  .kicker{font-weight:700;font-size:30px;letter-spacing:.30em;color:var(--accent);text-transform:uppercase}
  .huge{font-weight:800;letter-spacing:-.045em;line-height:.88;font-size:150px}
  .lead{font-weight:700;font-size:58px;line-height:1.18;letter-spacing:-.02em;margin-top:32px}
  /* Captions sit clear of the platform UI: bottom ~15% and right ~10% are covered. */
  #cap{position:absolute;left:60px;right:60px;top:1390px;height:340px;display:flex;
    align-items:center;justify-content:center;flex-wrap:wrap;gap:0 19px;text-align:center;
    align-content:center}
  #cap .w{font-weight:800;font-size:62px;line-height:1.22;letter-spacing:-.02em;color:#9AA3B2}
  #cap .w.on{color:#FFF} #cap .w.hit{color:var(--accent)}
</style></head><body>
<div id="sky"></div><div id="vig"></div>
<div class="stage" id="stage"></div><div id="cap"></div>
<script>
var D = /*__DATA__*/ null;
var T = parseFloat(new URLSearchParams(location.search).get('t') || '0');

function clamp(v,a,b){a=a===undefined?0:a;b=b===undefined?1:b;return v<a?a:(v>b?b:v);}
function outCubic(p){p=clamp(p);return 1-Math.pow(1-p,3);}
function rise(start,dur){return outCubic((T-start)/dur);}
function spike(at,dur){var p=(T-at)/dur;return (p<0||p>1)?0:Math.pow(1-p,2.2);}
/* Hand-off, not dissolve: the outgoing beat is gone AT the boundary and the
   incoming one starts there. A centred crossfade shows both at once, which over
   a text-heavy screen reads as muddy rather than smooth. */
function win(a,b,d){
  d = d || 0.42;
  var fin  = (a <= 0.02)            ? 1 : outCubic((T-a)/d);
  var fout = (b >= D.duration-0.02) ? 1 : 1-outCubic((T-(b-d))/d);
  return clamp(Math.min(fin,fout));
}

/* a slow storm-ish ground that never repeats inside one run */
document.getElementById('sky').style.background =
  'radial-gradient('+(108+10*Math.sin(T*.09))+'% 74% at '
  +(50+16*Math.sin(T*0.16))+'% '+(26+9*Math.cos(T*0.11))+'%,'
  +' rgba(38,54,86,.95) 0%, rgba(19,26,44,1) 46%, #06070B 100%)';

var stage = document.getElementById('stage');
function layer(o, html){
  if (o <= 0.001) return null;
  var el = document.createElement('div');
  el.className='layer'; el.style.opacity=o.toFixed(3); el.innerHTML=html;
  stage.appendChild(el); return el;
}

for (var i=0;i<D.beats.length;i++){
  var B = D.beats[i], o = win(B['in'], B.out);
  if (!o) continue;
  var el = layer(o,
    '<div style="width:100%;text-align:center">'
    + (B.kicker ? '<div class="kicker" id="k">'+B.kicker+'</div>' : '')
    + (B.head   ? '<div class="huge" id="h" style="margin-top:26px">'+B.head+'</div>' : '')
    + (B.lead   ? '<div class="lead" id="l">'+B.lead+'</div>' : '')
    + '</div>');
  var k=el.querySelector('#k'), h=el.querySelector('#h'), l=el.querySelector('#l');
  if (k) k.style.opacity = rise(B['in']+0.12, .60);
  if (h){ h.style.opacity = rise(B['in']+0.30, .70);
          h.style.transform = 'translateY('+((1-rise(B['in']+0.30,.95))*30).toFixed(1)+'px)'; }
  if (l){ l.style.opacity = rise(B['in']+0.62, .70);
          l.style.transform = 'translateY('+((1-rise(B['in']+0.62,.95))*24).toFixed(1)+'px)'; }
}

/* captions: one chunk always on screen, current word highlighted */
(function(){
  var cap = document.getElementById('cap'), cur = null;
  var last = D.chunks[D.chunks.length-1];
  for (var i=0;i<D.chunks.length;i++){
    var c = D.chunks[i];
    if (T >= c.start-0.10 && T < c.until){ cur=c; break; }
  }
  if (!cur){ cap.style.opacity=0; return; }
  var o = outCubic((T-(cur.start-0.10))/0.14);
  if (cur === last) o = Math.min(o, 1-outCubic((T-(cur.end+0.25))/0.35));
  cap.style.opacity = clamp(o).toFixed(3);
  var html='';
  for (var k=0;k<cur.words.length;k++){
    var w = cur.words[k];
    var spoken = T >= w.s, live = T >= w.s && T <= w.e + 0.06;
    html += '<span class="w'+(spoken?' on':'')+(live?' hit':'')+'"'
         +  ' style="display:inline-block;transform:scale('
         +  (live ? (1+spike(w.s,0.20)*0.09) : 1).toFixed(3)+')">'
         +  w.w.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span>';
  }
  cap.innerHTML = html;
})();
</script></body></html>
"""

TEMPLATE = TEMPLATE.replace("FONTDIR", FONT_DIR.replace("\\", "/"))

if __name__ == "__main__":
    main()
