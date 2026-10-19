"""Build scene.html for the 'Normaler Samstag' Short.

The HTML is a PURE FUNCTION of ?t=<seconds> -- no CSS animation, no rAF. That
is what lets the renderer sample any frame in any order and cache it. Run this
whenever words.json or the beat table below changes.

    python projects/samstag/scene/build_scene.py
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "assets", "fonts"))

# --- caption grouping ---------------------------------------------------------
# German compound nouns blow any character cap, so the budget is in ems of
# advance width, approximated by character class. Cheap, and good enough at this
# size because the band is 940 px wide with only 2 lines.
WIDE = set("mwMW@")
NARROW = set("iljItfr.,!?:;'")


def width_units(text: str) -> float:
    u = 0.0
    for ch in text:
        u += 1.35 if ch in WIDE else 0.48 if ch in NARROW else 1.0
    return u


def group_words(words: list[dict], budget: float = 21.0, max_words: int = 5,
                max_dur: float = 2.0) -> list[dict]:
    """Word runs that fit the caption band and break on punctuation."""
    chunks: list[dict] = []
    cur: list[dict] = []

    def flush() -> None:
        if not cur:
            return
        chunks.append({
            "start": cur[0]["start"],
            "end": cur[-1]["end"],
            "words": [{"w": w["word"], "s": w["start"], "e": w["end"]} for w in cur],
        })
        cur.clear()

    for w in words:
        trial = cur + [w]
        text = " ".join(x["word"] for x in trial)
        too_wide = width_units(text) > budget
        too_long = trial[-1]["end"] - trial[0]["start"] > max_dur
        too_many = len(trial) > max_words
        # A gap this large is a real pause; never straddle it.
        gap = (w["start"] - cur[-1]["end"]) if cur else 0.0
        if cur and (too_wide or too_long or too_many or gap > 0.45):
            flush()
        cur.append(w)
        if re.search(r"[.!?:]$", w["word"]):
            flush()
    flush()
    # Hold each chunk until the next one starts. Fading out at the chunk's own
    # end leaves a gap in which no caption is on screen, which reads as a blink
    # several times a sentence.
    for i, c in enumerate(chunks):
        c["until"] = chunks[i + 1]["start"] if i + 1 < len(chunks) else c["end"] + 0.70
    return chunks


# --- the beat table -----------------------------------------------------------
# Times are anchored to the words that actually carry them (verified against
# words.json), not guessed from the sentence grid.
STRIKES = [
    {"n": 1, "t": 13.75, "place": "Wachturm",       "hurt": "Bein",                 "hair": False},
    {"n": 2, "t": 16.13, "place": "Auto",           "hurt": "Augenbrauen + Haare",  "hair": True},
    {"n": 3, "t": 17.90, "place": "Vorgarten",      "hurt": "Schulter",             "hair": False},
    {"n": 4, "t": 18.89, "place": "Ranger-Station", "hurt": "—",               "hair": False},
    {"n": 5, "t": 35.35, "place": "Neben dem Auto", "hurt": "Haare",                "hair": True},
    {"n": 6, "t": 38.25, "place": "Unterwegs",      "hurt": "Knöchel + Haare", "hair": True},
    {"n": 7, "t": 40.30, "place": "Beim Angeln",    "hurt": "Oberkörper + Haare", "hair": True},
]

SCENES = [
    {"id": "hook",    "in": 0.00,  "out": 7.30},
    {"id": "zahl",    "in": 7.30,  "out": 12.24},
    {"id": "log",     "in": 12.24, "out": 20.00},
    {"id": "wasser",  "in": 20.00, "out": 25.57},
    {"id": "abstand", "in": 25.57, "out": 30.00},
    {"id": "sturm",   "in": 30.00, "out": 34.80},
    {"id": "log2",    "in": 34.80, "out": 43.20},
    {"id": "baer",    "in": 43.20, "out": 52.26},
    {"id": "ende",    "in": 52.26, "out": 54.20},
]

# Hair catches fire on these words. The narration builds this running gag on its
# own; the tally just makes it countable.
HAIR_EVENTS = [17.36, 37.57, 39.63, 42.83]


def main() -> None:
    with open(os.path.join(HERE, "words.json"), "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    data = {
        "duration": 54.2,
        "chunks": group_words(doc["words"]),
        "strikes": STRIKES,
        "scenes": SCENES,
        "hair": HAIR_EVENTS,
        "fontDir": FONT_DIR.replace("\\", "/"),
    }

    html = TEMPLATE.replace("/*__DATA__*/ null", json.dumps(data, ensure_ascii=False))
    out = os.path.join(HERE, "scene.html")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print(f"{out}  ({len(html)} bytes, {len(data['chunks'])} caption chunks)")


TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Normaler Samstag</title>
<style>
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-ExtraBold.ttf'); font-weight:800; }
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-Bold.ttf');      font-weight:700; }
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-SemiBold.ttf');  font-weight:600; }
  @font-face { font-family:'X'; src:url('FONTDIR/Inter-Regular.ttf');   font-weight:400; }

  :root{
    --ink:#FFFFFF; --dim:#8A93A6; --volt:#FFE14D; --cold:#8FD8FF; --hot:#FF7A45;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  html,body{width:1080px;height:1920px;overflow:hidden;background:#06070B}
  body{font-family:'X',system-ui,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}

  #sky{position:absolute;inset:0}
  #flash{position:absolute;inset:0;background:
     radial-gradient(120% 60% at 50% 18%, #FFFFFF 0%, #CFE9FF 32%, rgba(143,216,255,0) 70%);
     mix-blend-mode:screen;pointer-events:none}
  #vig{position:absolute;inset:0;pointer-events:none;
     background:radial-gradient(78% 58% at 50% 42%, rgba(0,0,0,0) 40%, rgba(0,0,0,.72) 100%)}
  #grain{position:absolute;inset:0;pointer-events:none;opacity:.05;mix-blend-mode:overlay}

  .stage{position:absolute;left:70px;right:70px;top:205px;height:1130px}
  .layer{position:absolute;inset:0;display:flex;align-items:center;
         justify-content:center;will-change:opacity,transform}
  .layer > div{width:100%}

  /* HUD */
  #hud{position:absolute;left:70px;right:70px;top:96px;display:flex;
       align-items:center;justify-content:space-between}
  .pill{display:flex;align-items:center;gap:14px;padding:14px 26px;border-radius:100px;
        background:rgba(255,255,255,.055);border:1px solid rgba(255,255,255,.10)}
  .pill b{font-weight:800;font-size:40px;letter-spacing:-.02em}
  .pill span{font-weight:600;font-size:24px;color:var(--dim);letter-spacing:.14em}
  .bolt{font-size:34px;line-height:1}

  /* type */
  .kicker{font-weight:700;font-size:30px;letter-spacing:.30em;color:var(--volt);
          text-transform:uppercase}
  .huge{font-weight:800;letter-spacing:-.045em;line-height:.86}
  .lead{font-weight:700;font-size:60px;line-height:1.16;letter-spacing:-.02em}
  .sub{font-weight:600;font-size:40px;line-height:1.32;color:var(--dim)}

  /* strike log */
  .row{position:absolute;left:0;right:0;height:126px;display:flex;align-items:center;gap:30px}
  .num{width:104px;height:104px;flex:0 0 104px;border-radius:26px;display:flex;
       align-items:center;justify-content:center;font-weight:800;font-size:50px;
       background:rgba(255,225,77,.12);border:1px solid rgba(255,225,77,.34);color:var(--volt)}
  .place{font-weight:800;font-size:50px;letter-spacing:-.02em}
  .hurt{font-weight:600;font-size:33px;color:var(--dim);margin-top:6px}
  .fire{color:var(--hot)}

  /* captions */
  #cap{position:absolute;left:60px;right:60px;top:1390px;height:340px;
       display:flex;align-items:center;justify-content:center;flex-wrap:wrap;
       gap:0 19px;text-align:center;align-content:center}
  #cap .w{font-weight:800;font-size:62px;line-height:1.22;letter-spacing:-.02em;
          color:#9AA3B2;transition:none}
  #cap .w.on{color:#FFFFFF}
  #cap .w.hit{color:var(--volt)}
</style>
</head>
<body>
  <div id="sky"></div>
  <div id="flash"></div>
  <svg id="grain"><filter id="n"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="3" seed="7"/></filter><rect width="100%" height="100%" filter="url(#n)"/></svg>
  <div id="vig"></div>

  <div id="hud">
    <div class="pill"><span class="bolt">&#9889;</span><b id="cnt">0</b><span>/ 7 TREFFER</span></div>
    <div class="pill" id="hairPill"><span class="bolt">&#128293;</span><b id="hcnt">0</b><span>&times; HAARE</span></div>
  </div>

  <div class="stage" id="stage"></div>
  <div id="cap"></div>

<script>
var D = /*__DATA__*/ null;
var T = parseFloat(new URLSearchParams(location.search).get('t') || '0');

/* ---- pure easing helpers: every visual is f(T) ---- */
function clamp(v,a,b){ a=a===undefined?0:a; b=b===undefined?1:b; return v<a?a:(v>b?b:v); }
function outCubic(p){ p=clamp(p); return 1-Math.pow(1-p,3); }
function inOut(p){ p=clamp(p); return p<.5 ? 4*p*p*p : 1-Math.pow(-2*p+2,3)/2; }
/* rise: 0 before start, eased 0->1 over dur */
function rise(start,dur){ return outCubic((T-start)/dur); }
/* Crossfade CENTRED on the scene boundary. The previous version held a scene
   at full opacity until b and only then faded out, while the next scene had
   already faded in before a -- so for ~1.1 s both stood at full and you could
   still see the outgoing scene through the incoming one. Centring the fade on
   the shared boundary makes the two opacities sum to ~1 instead. */
function win(a, b, d){
  d = d || 0.42;
  /* Hand-off, not dissolve: the outgoing scene is fully gone AT the boundary and
     the incoming one starts there. A centred crossfade still shows both at once,
     and over a text-heavy screen that reads as muddy rather than smooth. */
  var fin  = (a <= 0.02)            ? 1 : outCubic((T - a) / d);
  var fout = (b >= D.duration-0.02) ? 1 : 1 - outCubic((T - (b - d)) / d);
  return clamp(Math.min(fin, fout));
}
/* decaying spike, used for the lightning flashes */
function spike(at,dur){ var p=(T-at)/dur; return (p<0||p>1)?0:Math.pow(1-p,2.2); }

/* ---- sky: a slow storm that never repeats within the run ---- */
(function(){
  var d = T/D.duration;
  var x = 50 + 16*Math.sin(T*0.16), y = 26 + 9*Math.cos(T*0.11);
  var lift = 0.05 + 0.05*Math.sin(T*0.21);
  document.getElementById('sky').style.background =
    'radial-gradient('+ (108+10*Math.sin(T*.09)) +'% 74% at '+x+'% '+y+'%,'
    + ' rgba(38,54,86,'+(0.95).toFixed(3)+') 0%,'
    + ' rgba(19,26,44,1) 46%, #06070B 100%)';
  document.body.style.background = '#06070B';
  document.getElementById('vig').style.opacity = (0.85 + lift).toFixed(3);
})();

/* ---- lightning flashes on the strike beats ---- */
(function(){
  var f = 0;
  for (var i=0;i<D.strikes.length;i++) f = Math.max(f, spike(D.strikes[i].t, 0.42));
  for (var j=0;j<D.hair.length;j++)    f = Math.max(f, spike(D.hair[j], 0.30)*0.55);
  document.getElementById('flash').style.opacity = (f*0.60).toFixed(4);
})();

/* ---- HUD counters ---- */
(function(){
  var n=0; for (var i=0;i<D.strikes.length;i++) if (T>=D.strikes[i].t) n++;
  var h=0; for (var j=0;j<D.hair.length;j++)    if (T>=D.hair[j]) h++;
  document.getElementById('cnt').textContent  = n;
  document.getElementById('hcnt').textContent = h;
  /* the counters only matter once counting has started */
  var vis = win(11.4, 43.6, .50);
  document.getElementById('hud').style.opacity = vis.toFixed(3);
  /* a beat of scale on the frame a strike lands */
  var k=1; for (var m=0;m<D.strikes.length;m++) k = Math.max(k, 1+spike(D.strikes[m].t,.45)*0.13);
  document.getElementById('cnt').style.transform = 'scale('+k.toFixed(3)+')';
  document.getElementById('cnt').style.display='inline-block';
  var hk=1; for (var q=0;q<D.hair.length;q++) hk = Math.max(hk, 1+spike(D.hair[q],.40)*0.16);
  document.getElementById('hairPill').style.transform='scale('+hk.toFixed(3)+')';
  document.getElementById('hairPill').style.opacity = (T>=D.hair[0]-.2?1:0.28).toFixed(2);
})();

/* ---- stage: build every layer, then set opacity from its window ---- */
var stage = document.getElementById('stage');
function layer(o, html){
  if (o <= 0.001) return null;
  var el = document.createElement('div');
  el.className='layer'; el.style.opacity=o.toFixed(3); el.innerHTML=html;
  stage.appendChild(el); return el;
}

/* HOOK ------------------------------------------------------------------ */
(function(){
  var o = win(0, 7.30, .50); if (!o) return;
  var el = layer(o,
    '<div style="width:100%">'
    + '<div class="kicker" id="k1">Eine wahre Geschichte</div>'
    + '<div class="huge" id="n1" style="font-size:300px;margin-top:26px">ROY</div>'
    + '<div class="lead" id="l1" style="margin-top:34px;max-width:840px">'
    + 'Manche Menschen haben Pech.<br>Und dann gibt es <span style="color:var(--volt)">ihn</span>.</div>'
    + '<div class="huge" id="a1" style="font-size:96px;margin-top:56px;color:var(--cold)">'
    + 'ABSCHIEBEN<br>NACH ASGARD</div>'
    + '</div>');
  el.querySelector('#k1').style.opacity = rise(0.15,.7);
  var n1=el.querySelector('#n1');
  n1.style.opacity   = rise(0.45,.8);
  n1.style.transform = 'translateY('+((1-rise(0.45,1.1))*40).toFixed(1)+'px) scale('+(0.97+0.03*rise(0.45,1.4)).toFixed(3)+')';
  var l1=el.querySelector('#l1');
  l1.style.opacity   = rise(0.95,.8);
  l1.style.transform = 'translateY('+((1-rise(0.95,1.0))*26).toFixed(1)+'px)';
  var a1=el.querySelector('#a1');
  /* the Asgard punch lands on the word, not before it */
  a1.style.opacity   = rise(6.62,.42);
  a1.style.transform = 'translateY('+((1-rise(6.62,.6))*22).toFixed(1)+'px) rotate('+((1-rise(6.62,.8))*-2.5).toFixed(2)+'deg)';
})();

/* THE NUMBER ------------------------------------------------------------ */
(function(){
  var o = win(7.30, 12.24, .50); if (!o) return;
  var el = layer(o,
    '<div style="width:100%">'
    + '<div class="kicker">Offiziell best&auml;tigt</div>'
    + '<div class="huge" id="big" style="font-size:520px;margin-top:10px;color:var(--volt)">7</div>'
    + '<div class="sub" id="s2" style="margin-top:-10px;font-size:52px;color:#FFFFFF">Blitzeinschl&auml;ge.<br>Ein Mensch.</div>'
    + '<div id="more" style="margin-top:62px;padding:30px 34px;border-radius:26px;'
    + 'background:rgba(143,216,255,.08);border:1px solid rgba(143,216,255,.22);max-width:880px">'
    + '<div class="sub" style="font-size:38px;color:var(--cold)">Roy sagte: es waren mehr.</div>'
    + '<div class="sub" style="font-size:32px;margin-top:8px">Nur nachweisen konnte er es nicht.</div>'
    + '</div></div>');
  var big=el.querySelector('#big');
  big.style.opacity   = rise(7.55,.5);
  big.style.transform = 'scale('+(0.86+0.14*outCubic((T-7.55)/0.9)).toFixed(3)+')';
  el.querySelector('#s2').style.opacity = rise(8.15,.6);
  var more=el.querySelector('#more');
  more.style.opacity   = rise(9.85,.7);
  more.style.transform = 'translateY('+((1-rise(9.85,.9))*30).toFixed(1)+'px)';
})();

/* STRIKE LOG (both passes share one builder so rows keep their state) ---- */
function buildLog(o, upto){
  if (!o) return;
  var rows='';
  for (var i=0;i<D.strikes.length;i++){
    var s=D.strikes[i];
    if (i>=upto) break;
    var app = rise(s.t, .55);
    if (app<=0.002) continue;
    var y = i*136 + (1-app)*26;
    rows += '<div class="row" style="top:'+y.toFixed(1)+'px;opacity:'+app.toFixed(3)+'">'
         +  '<div class="num" style="transform:scale('+(1+spike(s.t,.5)*0.10).toFixed(3)+')">'
         +  ('0'+s.n)+'</div><div><div class="place">'+s.place+'</div>'
         +  '<div class="hurt'+(s.hair?' fire':'')+'">'+s.hurt+'</div></div></div>';
  }
  layer(o, '<div style="width:100%">'
        + '<div class="kicker" style="margin-bottom:36px">Das Protokoll</div>'
        + '<div style="position:relative;height:960px">'+rows+'</div></div>');
}
buildLog(win(12.24, 20.00, .50), 4);
buildLog(win(34.80, 43.20, .50), 7);

/* NOTE CARDS: Wasser / Abstand / Sturm ---------------------------------- */
/* These beats are ALREADY spoken. Putting the same sentence on screen wastes
   the frame and reads as auto-generated, so they carry no text at all -- the
   motion has to say it. Text is reserved for what the voice does NOT give you:
   names, numbers, structure, and punch-line setups. */
function graphic(a, b, build){
  var o = win(a, b, .50); if (!o) return;
  var p = outCubic((T - a - 0.15) / (b - a - 0.9));   /* 0 -> 1 across the beat */
  /* The artwork is drawn in a 900x640 space but the stage is 940x1130, so it
     reads small unless it is blown up about the centre. */
  layer(o, '<div style="width:100%;text-align:center">'
    + '<svg viewBox="0 0 900 640" width="900" height="640" '
    + 'style="max-width:100%;overflow:visible">'
    + '<g transform="translate(450,320) scale(1.24) translate(-450,-320)">'
    + build(p) + '</g></svg></div>');
}

/* a walking stick figure, deliberately plain so the motion reads, not the art */
function fig(x, y, s, col, op){
  return '<g transform="translate('+x.toFixed(1)+','+y.toFixed(1)+') scale('+s.toFixed(3)+')" '
    + 'opacity="'+op.toFixed(3)+'" fill="none" stroke="'+col+'" stroke-width="11" '
    + 'stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="0" cy="-72" r="24" fill="'+col+'" stroke="none"/>'
    + '<path d="M0,-46 L0,10"/><path d="M0,-30 L-26,2 M0,-30 L26,2"/>'
    + '<path d="M0,10 L-22,58 M0,10 L22,58"/></g>';
}
function bolt(x, y, s, col, op){
  return '<path d="M6,-60 L-26,4 L-2,4 L-10,60 L26,-8 L2,-8 Z" '
    + 'transform="translate('+x.toFixed(1)+','+y.toFixed(1)+') scale('+s.toFixed(3)+')" '
    + 'fill="'+col+'" opacity="'+op.toFixed(3)+'"/>';
}

/* WASSER -- the line is "he started CARRYING water with him", so the picture has
   to show carrying, not just dousing. Same stick-figure vocabulary as the
   ABSTAND beat: he walks, canister in hand, hair relights, he tips it over his
   own head, repeat. An earlier version showed only a flame being put out, which
   left the verb -- the actual point of the sentence -- undrawn. */
graphic(20.00, 25.57, function(p){
  var s = '';
  var COL = '#C9D2E2', CX = 430;
  var ph  = T * 4.6;                       /* walk cycle */
  var sw  = Math.sin(ph);
  var hip = 450 - Math.abs(Math.cos(ph)) * 9;
  var sho = hip - 142, headY = hip - 200;

  /* Burns -> he tips the canister -> out -> it catches again. The flame never
     drops below 0.28: an ember reads as nothing at this size, and the beat then
     has seconds of dead frame. */
  var cyc = (T - 20.0) / 2.60; cyc = cyc - Math.floor(cyc);
  var pour = (cyc > 0.42 && cyc < 0.66);
  var f = cyc < 0.42 ? (0.70 + 0.30 * (cyc / 0.42))
        : cyc < 0.66 ? (1.00 - 0.72 * ((cyc - 0.42) / 0.24))
        : (0.28 + 0.42 * ((cyc - 0.66) / 0.34));

  /* ground marks scrolling past, so walking-in-place still reads as walking */
  for (var g = 0; g < 7; g++){
    var gx = 900 - (((T * 165 + g * 150) % 1010));
    s += '<path d="M' + gx.toFixed(1) + ',' + (hip + 128).toFixed(1) + ' h64" stroke="' + COL
      +  '" stroke-width="8" stroke-linecap="round" opacity=".18"/>';
  }

  /* flame on his head, with a little live flicker */
  var fl = f * (1 + 0.07 * Math.sin(T * 13.0));
  s += '<g transform="translate(' + CX + ',' + (headY - 58).toFixed(1) + ') scale('
    +  fl.toFixed(3) + ')">'
    +  '<path d="M0,-104 C48,-52 66,-24 66,7 A66,66 0 1,1 -66,7 C-66,-30 -30,-48 0,-104 Z" fill="#FF7A45"/>'
    +  '<path d="M0,-48 C24,-17 32,-4 32,11 A32,32 0 1,1 -32,11 C-32,-2 -15,-20 0,-48 Z" fill="#FFE14D"/></g>';

  /* the poured stream -- held well clear of the head so it never overlaps it */
  if (pour){
    for (var d = 0; d < 7; d++){
      var q = ((T * 2.4 + d * 0.143) % 1);
      var ax = CX + 176 - q * 176;
      var ay = (headY - 96) + q * q * 104;
      s += '<circle cx="' + ax.toFixed(1) + '" cy="' + ay.toFixed(1) + '" r="'
        +  (13 + q * 7).toFixed(1) + '" fill="#8FD8FF" opacity="' + (0.95 - q * 0.25).toFixed(2) + '"/>';
    }
  }

  s += '<g fill="none" stroke="' + COL + '" stroke-width="17" stroke-linecap="round" '
    +  'stroke-linejoin="round">';
  s += '<circle cx="' + CX + '" cy="' + headY.toFixed(1) + '" r="46" fill="' + COL + '" stroke="none"/>';
  s += '<path d="M' + CX + ',' + (headY + 46).toFixed(1) + ' L' + CX + ',' + hip.toFixed(1) + '"/>';
  /* legs -- a wide swing, or it does not read as walking */
  var la = 0.58 * sw, ra = -0.58 * sw;
  s += '<path d="M' + CX + ',' + hip.toFixed(1) + ' L' + (CX + Math.sin(la) * 118).toFixed(1)
    +  ',' + (hip + Math.cos(la) * 118).toFixed(1) + '"/>';
  s += '<path d="M' + CX + ',' + hip.toFixed(1) + ' L' + (CX + Math.sin(ra) * 118).toFixed(1)
    +  ',' + (hip + Math.cos(ra) * 118).toFixed(1) + '"/>';
  /* back arm swings; the carrying arm stays put under the load */
  s += '<path d="M' + CX + ',' + sho.toFixed(1) + ' L' + (CX - 74 + sw * 22).toFixed(1)
    +  ',' + (sho + 86).toFixed(1) + '"/>';
  s += '<path d="M' + CX + ',' + sho.toFixed(1) + ' L' + (CX + 130).toFixed(1)
    +  ',' + (pour ? (sho - 84) : (sho + 70)).toFixed(1) + '"/>';
  s += '</g>';

  /* the canister -- out to the side, tipped over while pouring */
  var canX = CX + 176;
  var canY = pour ? (headY - 96) : (sho + 96);
  var rot  = pour ? -142 : 0;
  s += '<g transform="translate(' + canX + ',' + canY.toFixed(1) + ') rotate(' + rot + ')">'
    +  '<rect x="-42" y="-46" width="84" height="102" rx="16" fill="none" stroke="#8FD8FF" stroke-width="14"/>'
    +  '<rect x="-18" y="-68" width="36" height="24" rx="8" fill="none" stroke="#8FD8FF" stroke-width="13"/>'
    +  '<path d="M-22,-10 h44" stroke="#8FD8FF" stroke-width="10" opacity=".5"/></g>';
  return s;
});

/* ABSTAND -- everyone slides away from the one with the bolt over his head. */
graphic(25.57, 30.00, function(p){
  /* Polar layout against an ELLIPSE, so "outside the ring" is guaranteed by
     construction. The earlier cartesian version squashed y independently of the
     ring, which put one figure visibly inside the danger circle. */
  var s = '', spread = outCubic(p) * 150;
  var CX = 450, CY = 330, SQUASH = 0.62;
  var rx = 118 + spread*0.55, ry = rx*SQUASH;
  var ANG = [186, 152, 356, 26, 90];            /* degrees; 270 is left free for the bolt */
  for (var i=0;i<ANG.length;i++){
    var a2 = ANG[i]*Math.PI/180;
    var r  = rx + 74 + spread*0.22;             /* always clear of the ellipse */
    var x  = CX + Math.cos(a2)*r;
    var y  = CY + Math.sin(a2)*r*SQUASH;
    s += fig(x, y, 0.84, '#5C6679', 0.50 + 0.28*(1-outCubic(p)));
  }
  s += '<ellipse cx="'+CX+'" cy="'+CY+'" rx="'+rx.toFixed(1)+'" ry="'+ry.toFixed(1)+'" '
    +  'fill="none" stroke="#FFE14D" stroke-width="4" stroke-dasharray="14 18" '
    +  'opacity="'+(0.42*outCubic(p)).toFixed(3)+'"/>';
  s += fig(CX, CY+42, 1.16, '#FFE14D', 1);
  s += bolt(CX, CY-154, 1.25 + 0.12*Math.sin(T*7), '#FFE14D',
            0.55 + 0.45*Math.abs(Math.sin(T*3.1)));
  return s;
});

/* STURM -- he drives off; the cloud comes with him. The joke is the chase. */
graphic(30.00, 34.80, function(p){
  var s = '', drive = outCubic(p);
  /* cloud, always a little behind the car */
  var cx = 210 + drive*150;
  s += '<g transform="translate('+cx.toFixed(1)+',210)" opacity="'+Math.min(1,p*5).toFixed(3)+'">'
    +  '<circle cx="-70" cy="10" r="62" fill="#38455F"/>'
    +  '<circle cx="10" cy="-26" r="80" fill="#38455F"/>'
    +  '<circle cx="90" cy="14" r="58" fill="#38455F"/>'
    +  '<rect x="-74" y="30" width="176" height="52" rx="26" fill="#38455F"/></g>';
  s += bolt(cx + 22, 330, 1.15, '#FFE14D',
            (0.25 + 0.75*Math.abs(Math.sin(T*4.6))) * clamp(p*2));
  /* car pulling away to the right */
  var carx = 452 + drive*128;
  s += '<g transform="translate('+carx.toFixed(1)+',470)" opacity="'+Math.min(1,p*4).toFixed(3)+'">'
    +  '<path d="M-130,20 L-104,-34 L58,-34 L104,20 Z" fill="none" stroke="#C9D2E2" stroke-width="11" '
    +  'stroke-linejoin="round"/>'
    +  '<rect x="-140" y="18" width="256" height="34" rx="17" fill="#C9D2E2"/>'
    +  '<circle cx="-78" cy="62" r="26" fill="none" stroke="#C9D2E2" stroke-width="11"/>'
    +  '<circle cx="62" cy="62" r="26" fill="none" stroke="#C9D2E2" stroke-width="11"/></g>';
  /* speed lines behind it */
  for (var i=0;i<3;i++){
    var op = 0.30*drive*(1-i*0.26);
    s += '<path d="M'+(carx-190-i*54)+',' +(438+i*30)+ ' h'+(58+i*16)+'" stroke="#C9D2E2" '
      +  'stroke-width="7" stroke-linecap="round" opacity="'+op.toFixed(3)+'"/>';
  }
  return s;
});

/* THE BEAR -------------------------------------------------------------- */
function bearArt(){
  var flick = Math.sin(T*9.0), flick2 = Math.sin(T*11.7 + 1.3);
  var s = '';
  /* burning hair first, so the head sits over the flame roots */
  for (var i=0;i<3;i++){
    var x = 260 + (i-1)*66, w = 1 + 0.10*(i===1?flick:flick2);
    var h = (i===1 ? 132 : 100) * (1 + 0.11*(i===1?flick2:flick));
    s += '<path d="M0,0 C'+(34*w)+','+(-h*0.5)+' '+(44*w)+','+(-h*0.75)+' 0,'+(-h)
      +  ' C'+(-44*w)+','+(-h*0.75)+' '+(-34*w)+','+(-h*0.5)+' 0,0 Z" '
      +  'transform="translate('+x+',126)" fill="#FF7A45" opacity=".92"/>';
    s += '<path d="M0,0 C'+(18*w)+','+(-h*0.3)+' '+(24*w)+','+(-h*0.45)+' 0,'+(-h*0.58)
      +  ' C'+(-24*w)+','+(-h*0.45)+' '+(-18*w)+','+(-h*0.3)+' 0,0 Z" '
      +  'transform="translate('+x+',126)" fill="#FFE14D"/>';
  }
  /* head */
  s += '<circle cx="176" cy="150" r="52" fill="#8A6A52"/>'
    +  '<circle cx="344" cy="150" r="52" fill="#8A6A52"/>'
    +  '<circle cx="176" cy="150" r="26" fill="#6B5140"/>'
    +  '<circle cx="344" cy="150" r="26" fill="#6B5140"/>'
    +  '<circle cx="260" cy="232" r="118" fill="#8A6A52"/>'
    +  '<ellipse cx="260" cy="276" rx="62" ry="48" fill="#C6A98E"/>'
    +  '<ellipse cx="260" cy="252" rx="22" ry="16" fill="#3A2B22"/>'
    +  '<circle cx="216" cy="208" r="13" fill="#2A1F19"/>'
    +  '<circle cx="304" cy="208" r="13" fill="#2A1F19"/>'
    +  '<path d="M232,296 Q260,314 288,296" stroke="#3A2B22" stroke-width="9" '
    +  'fill="none" stroke-linecap="round"/>';
  return s;
}
(function(){
  var o = win(43.20, 52.26, .50); if (!o) return;
  var el = layer(o,
    '<div style="width:100%;text-align:center">'
    + '<div class="kicker" id="k4">Sieben Treffer — geschenkt.</div>'
    + '<div class="huge" id="h4" style="font-size:112px;margin-top:40px">ABER<br>DANN<br>SAGT ER:</div>'
    /* The claim itself is spoken twice over -- drawing it as well would just be
       the sentence again. The picture carries it: a bear, and hair on fire. */
    + '<div id="claim" style="margin-top:48px;padding:34px;border-radius:30px;'
    + 'background:rgba(255,122,69,.10);border:1px solid rgba(255,122,69,.30)">'
    + '<svg id="bear" viewBox="0 0 520 380" width="520" height="380" '
    + 'style="max-width:100%;overflow:visible">' + bearArt() + '</svg>'
    + '</div></div>');
  el.querySelector('#k4').style.opacity = rise(43.45,.7);
  var h4=el.querySelector('#h4');
  h4.style.opacity   = rise(46.55,.6);
  h4.style.transform = 'translateY('+((1-rise(46.55,.9))*26).toFixed(1)+'px)';
  var cl=el.querySelector('#claim');
  cl.style.opacity   = rise(49.40,.55);
  cl.style.transform = 'translateY('+((1-rise(49.40,.8))*30).toFixed(1)+'px)';
  el.querySelector('#bear').style.transform =
    'scale('+(0.86+0.14*outCubic((T-50.30)/0.7)).toFixed(3)+')';
})();

/* CLOSER ---------------------------------------------------------------- */
(function(){
  var o = win(52.26, 54.20, .50); if (!o) return;
  var el = layer(o,
    '<div style="width:100%;text-align:center">'
    + '<div class="huge" id="e1" style="font-size:96px;line-height:1.06">Ich hasse es,<br>wenn das<br>passiert.</div>'
    + '<div class="kicker" id="e2" style="margin-top:64px">Roy Sullivan &middot; 1912–1983</div></div>');
  var e1=el.querySelector('#e1');
  e1.style.opacity   = rise(52.36,.55);
  e1.style.transform = 'scale('+(0.965+0.035*rise(52.36,1.1)).toFixed(3)+')';
  el.querySelector('#e2').style.opacity = rise(53.35,.6);
})();

/* CAPTIONS -------------------------------------------------------------- */
(function(){
  var cap = document.getElementById('cap');
  var cur = null, last = D.chunks[D.chunks.length-1];
  for (var i=0;i<D.chunks.length;i++){
    var c=D.chunks[i];
    if (T >= c.start-0.10 && T < c.until){ cur=c; break; }
  }
  if (!cur){ cap.style.opacity=0; return; }
  /* fade in on the swap; only the final chunk fades out */
  var o = outCubic((T-(cur.start-0.10))/0.14);
  if (cur === last) o = Math.min(o, 1-outCubic((T-(cur.end+0.25))/0.35));
  cap.style.opacity = clamp(o).toFixed(3);
  var html='';
  for (var k=0;k<cur.words.length;k++){
    var w=cur.words[k];
    var spoken = T >= w.s;
    var live   = T >= w.s && T <= w.e + 0.06;
    var pop    = live ? 1+spike(w.s,0.20)*0.09 : 1;
    html += '<span class="w'+(spoken?' on':'')+(live?' hit':'')+'"'
         +  ' style="transform:scale('+pop.toFixed(3)+');display:inline-block">'
         +  w.w.replace(/&/g,'&amp;').replace(/</g,'&lt;') + '</span>';
  }
  cap.innerHTML = html;
})();
</script>
</body>
</html>
"""

TEMPLATE = TEMPLATE.replace("FONTDIR", FONT_DIR.replace("\\", "/"))

if __name__ == "__main__":
    main()
