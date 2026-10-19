# Videos schneiden mit Claude

Praktische Anleitung auf Deutsch: vom Rohmaterial bis zur fertigen Datei.
Die technischen Details stehen in [CLAUDE.md](CLAUDE.md) und [docs/](docs/).

Getestet auf Windows 11, Python 3.14.2, ffmpeg 8.1.1, RTX 4070 SUPER.

---

## Was ist das überhaupt?

Du redest, ich schneide. Du sagst mir in normaler Sprache, was passieren soll —
„schneid die Ähms raus", „kürz das auf sechs Minuten", „der Schnitt bei Minute
drei kommt 200 Millisekunden zu spät" — und ich schreibe die Entscheidung in eine
einzige Datei, `project.json`. Aus dieser Datei wird gerendert.

Das ist der ganze Trick, und er hat drei angenehme Folgen:

1. **Nichts wird jemals gelöscht.** Ein rausgeschnittenes „ähm", ein verworfener
   Take, ein gestrichenes Thema — alles bleibt in der Datei stehen, nur mit
   `"enabled": false` und einer Begründung. Jede automatische Entscheidung ist
   ein einziges Wort davon entfernt, rückgängig gemacht zu werden.
2. **Kleine Änderungen sind billig.** „Der Schnitt soll 200 ms früher" ändert
   *eine* Zahl. Danach wird genau ein Segment neu gerendert, nicht das ganze
   Video. Minuten werden zu Sekunden.
3. **Du kannst nachlesen, warum.** Zu jedem Schnitt steht in der Datei, welche
   Regel ihn gemacht hat und wie sicher sie sich war.

Es gibt keine Timeline zum Anklicken. Es gibt die Datei, deine Sätze und das
Ergebnis zum Anschauen.

---

## Einmalig einrichten

Für die Hauptpipeline musst du **nichts** installieren. Sie läuft auf dem Python,
das schon auf dem Rechner ist, und benutzt ausschließlich die Standardbibliothek.
ffmpeg, Chrome, Node und `uv` sind ebenfalls schon da.

Eine einzige Sache fehlt: die Transkription. WhisperX läuft nicht auf Python
3.14, deshalb bekommt es eine eigene, abgeschottete Umgebung. Dafür gibt es ein
Skript, das erst seinen kompletten Plan ausdruckt und dann fragt:

```powershell
cd <repo>

.\tools\setup.ps1 -DryRun     # zeigt nur, was passieren würde
.\tools\setup.ps1             # macht es wirklich
```

Das lädt beim ersten Mal ungefähr 4 GB an Paketen plus rund 3,5 GB an
Modelldateien. Danach nie wieder. Ein erneuter Aufruf ist ungefährlich — jeder
Schritt prüft, ob er schon erledigt ist.

Sag mir einfach **„richte die Transkription ein"** — dann mache ich das, prüfe
anschließend, ob die Grafikkarte wirklich angesprochen wird, und melde mich,
wenn etwas fehlt. Details und die Erklärung des berüchtigten
`cudnn_ops64_9.dll`-Fehlers stehen in [docs/setup.md](docs/setup.md).

> **Wichtig:** Ins Verzeichnis `assets/fonts/` gehören **nur** Schriftdateien.
> libass lädt dort jede einzelne Datei und stolpert über alles, was keine
> Schrift ist — auch über eine Readme.

---

## Die sieben Schritte

Du musst dir keinen davon merken. Frag jederzeit **„wo stehen wir?"** und ich
sage dir, in welchem Schritt das Projekt ist und was als Nächstes dran ist.

### Schritt 1 — Material einlesen

> „Leg ein Projekt an für Folge 42 und lies das Material aus C:\Videos\ep042 ein."

Ich lege `projects/ep042/` an und **transkodiere jede Datei** auf eine feste
Bildrate. Das ist kein Kopieren und es ist nicht optional: Handy- und
OBS-Aufnahmen haben oft eine schwankende Bildrate, und bei so einer Datei sind
sich nicht einmal zwei Werkzeuge einig, wie viele Bilder sie überhaupt hat. Nach
dem Einlesen gibt es genau eine Zeitbasis im Projekt, und ab da stimmt jede
Rechnung.

**Deine Originale werden nie angefasst.** Sie werden gelesen, nie beschrieben,
nie verschoben, nie gelöscht.

*Was du erwarten kannst:* etwa Echtzeit bis ein paar Mal schneller als Echtzeit,
je nach Material. Bei einer Stunde Rohmaterial ist das eine Kaffeepause.

### Schritt 2 — Transkript

> „Transkribier das."

WhisperX schreibt mit und legt für **jedes einzelne Wort** einen Anfangs- und
Endzeitpunkt fest — nicht geraten, sondern gegen das Audio ausgerichtet. Genau
diese Wortgrenzen sind später die Schnittpunkte.

*Was du erwarten kannst:* deutlich schneller als Echtzeit auf der Grafikkarte.
Beim allerersten Lauf kommen ein paar Minuten Modell-Download dazu.

*Eine Eigenheit:* Zahlen werden ausgeschrieben („zwanzig" statt „20"). Das ist
Absicht — anders bekommen Ziffern gar keine Zeitstempel und der Schnitt fliegt
dir um die Ohren. Für die Untertitel kann man einzelne Wörter überschreiben, sag
einfach **„im Untertitel soll da 20 stehen"**.

### Schritt 3 — Story

> „Worum geht es hier eigentlich? Kürz das auf sechs Minuten."

Das ist der Schritt, der aus einer Aufnahme ein *Video* macht. Ich lese das
Transkript, teile es in Themenblöcke, und entscheide, welche bleiben, welche raus
können, in welcher Reihenfolge sie stehen sollen und wo der Hook sitzt — der
Satz, der nach vorne gezogen wird, damit die ersten fünf Sekunden tragen.

Du bekommst eine Liste mit Begründung pro Block. Blöcke, bei denen ich mir unsicher
bin, markiere ich als „vielleicht" statt sie stillschweigend wegzuwerfen.

> „Block 4 bleibt drin, der ist mir wichtig. Und zieh den Schluss nach vorne."

*Warum das vor dem Rohschnitt kommt:* es ist Verschwendung, Versprecher in
Material zu suchen, das sowieso rausfliegt.

### Schritt 4 — Rough Cut

> „Mach den Rohschnitt."

Jetzt das Mechanische, innerhalb der behaltenen Blöcke:

- **Pausen kürzen.** Nicht alle. Eine Pause nach einem Punkt darf länger sein als
  eine mitten im Satz — sonst klingt der Schnitt wie ein Maschinengewehr. Was
  stehen bleibt, ist gestaffelt und richtet sich nach der Satzzeichensetzung.
- **Füllwörter.** In drei Stufen. „äh", „ähm", „hm" fliegen automatisch raus.
  „weißt du", „sozusagen", „quasi" fliegen raus, aber mit Sicherungen. **Modalpartikel
  fliegen nie automatisch raus** — „das ist halt so" heißt etwas anderes als „das
  ist so", und „also, fangen wir an" verliert ohne „also" seinen Anschluss. Die
  markiere ich nur und frage dich.
- **Versprecher und Neuanfänge.** Wenn du einen Satz noch einmal anfängst, finde
  ich beide Versionen und deaktiviere die erste. Deaktiviere, nicht lösche.
- **Sauber schneiden.** Jeder Schnitt wird auf einen Nulldurchgang der Wellenform
  gelegt und bekommt eine 20-ms-Blende. Ohne das klickt es hörbar.

*Was du erwarten kannst:* Sekunden. Es wird noch nichts gerendert, nur
entschieden. Mit **„zeig mir erst mal, was du rausschneiden würdest"** siehst du
die Liste, bevor irgendetwas passiert.

> **Ehrlich gesagt:** die Schwellenwerte dahinter sind noch nicht geeicht. Sie
> stammen aus Recherche und stimmen mit dem überein, was drei andere Werkzeuge
> tun — aber niemand hat einen damit gebauten Schnitt je in voller Länge
> angehört. Wenn dir etwas zu hart oder zu lasch vorkommt, sag es. Die Zahlen sind
> eher falsch als dein Ohr. Wie man sie richtig eicht, steht in
> [docs/cutting.md](docs/cutting.md).

### Schritt 5 — Grafiken

> „Blend meinen Namen als Bauchbinde ein, während ich mich vorstelle."

Grafiken sind HTML-Dateien. Jede berechnet ihr Aussehen aus einem einzigen Wert:
`t`, der Sekunde innerhalb der Einblendung. Deshalb sieht Bild 7 immer exakt
gleich aus, egal wie oft du renderst — und deshalb kann alles zwischengespeichert
werden.

Grafiken hängen an einem **Segment**, nicht an einer Uhrzeit. Wenn du davor noch
etwas rausschneidest, wandert die Bauchbinde automatisch mit.

*Was du erwarten kannst:* rund 0,4 Sekunden pro Bild. Eine sechs Sekunden lange
Einblendung ist etwa eine Minute Rechenzeit — beim ersten Mal. Danach kommt sie
aus dem Cache und kostet nichts.

### Schritt 6 — Untertitel

> „Brenn die Untertitel ein."

Die Untertitel werden nach **Pixelbreite** umgebrochen, nicht nach Zeichenzahl.
Das klingt nach einer Kleinigkeit und ist bei Deutsch der wichtigste Punkt
überhaupt: „Geschwindigkeitsbegrenzung" ist ein einziges, untrennbares Wort und
füllt bei 90 Pixel Schriftgröße knapp die halbe Bildbreite. Jede Zeichenobergrenze
bricht so ein Wort entweder falsch um oder lässt es aus dem Bild laufen.

Tippfehler korrigierst du direkt:

> „In Minute 2:14 steht ‚Gmbh', das muss ‚GmbH' heißen."

### Schritt 7 — Ton und Export

> „Leg Musik drunter, mach sie leiser wenn ich rede, und exportier das für YouTube."

Der gesamte Ton wird in **einem** Durchgang gebaut — nicht Stück für Stück und
dann zusammengeklebt. Beim Zusammenkleben verliert man an jeder Nahtstelle ein
paar Millisekunden, und über 200 Schnitte hörst du das als auseinanderlaufende
Lippensynchronität.

Danach Lautheit auf den Zielwert der Plattform (YouTube, TikTok, Instagram und
LinkedIn haben unterschiedliche), dann Bild und Ton genau einmal zusammenmuxen,
dann die Qualitätskontrolle.

*Was du erwarten kannst:* Das Bild wird stückweise gerendert und dann verlustfrei
zusammenkopiert, das geht fast sofort. Beim zweiten Export nach einer kleinen
Änderung wird fast alles aus dem Cache geholt.

Am Ende laufen acht Prüfungen: stimmt die Bildanzahl, stimmt die Samplezahl,
passen alle Segmente zusammen, ist die Grafik wirklich transparent, wurde die
richtige Schrift benutzt, stimmt die Lautheit, ist die Grafik nicht leer, sind
die Schwellenwerte geeicht. Du bekommst das Ergebnis als Liste — und zu jedem
Befund eine Datei zum Anschauen, nicht nur eine Zahl.

---

## Der andere Weg: nur Audio, kein Bildmaterial

Die sieben Schritte oben setzen voraus, dass du gefilmtes Material hast. Wenn du
nur ein Voiceover hast, gibt es einen zweiten Weg — dann **ist die Grafik das
Bild**, nicht eine Einblendung darüber. So ist `projects/samstag` entstanden.

```powershell
.\tools\ve.ps1 scene new samstag "%USERPROFILE%\Downloads\voiceover.mp3"
```

Das transkribiert die Datei (ohne Installation — über den Whisper-Filter, der in
ffmpeg schon drinsteckt), schreibt `words.json` mit Wort-Zeitstempeln und legt
dir eine `build_scene.py` zum Bearbeiten hin.

Darin steht eine **Beat-Tabelle**. Das ist der ganze Bearbeitungspunkt:

```python
BEATS = [
    {"id": "hook", "in": 0.00, "out": 6.00, "kicker": "Kicker",
     "head": "TITEL", "lead": "Ein Satz, den die Stimme so nicht sagt."},
]
```

Verankere jeden Beat an dem Wort, das ihn *trägt* — die Zeiten stehen in
`words.json`. Rätst du sie aus dem Satzraster, landet die Grafik neben der Zeile
statt darauf.

```powershell
.\tools\ve.ps1 scene render samstag     # jeden Frame einzeln aufnehmen
.\tools\ve.ps1 scene finish samstag     # gegen das Voiceover mischen
```

Der Render dauert bei 5 Arbeitern rund **4,5 Frames pro Sekunde** — eine Minute
Video also etwa sieben Minuten. Ändere eine Kleinigkeit und alles wird neu
aufgenommen, weil der Schlüssel die ganze Szenendatei ist. Plane das ein.

**Zwei Regeln, sonst geht es schief:**

1. Die Szene muss eine **reine Funktion von `?t=`** sein. Keine CSS-Animation,
   kein `requestAnimationFrame`, nichts, was die Uhr liest. Der Renderer spielt
   nichts ab, er fragt einzelne Zeitpunkte ab — alles Zeitgesteuerte würde an
   einer beliebigen Stelle erwischt und ruckeln.
2. **Bildtext muss etwas hinzufügen, nie wiederholen.** Sagt die Stimme es
   ohnehin gerade, nimm eine Grafik ohne Text oder gar nichts. Text ist für
   Namen, Zahlen, Struktur und Pointen-Setups da. Untertitel sind ausgenommen,
   die dienen dem Sehen ohne Ton.

Beides ausführlich in [docs/animation-toolkit.md](docs/animation-toolkit.md).

## Etwas ändern, nachdem es fertig ist

Das ist der eigentliche Punkt der ganzen Konstruktion. Sag einfach, was anders
sein soll:

> „Der Schnitt bei 3:12 kommt zu spät, 200 Millisekunden früher."

> „Das ‚also' am Anfang von Block 5 soll drinbleiben."

> „Der zweite Take war besser, nimm den."

> „Die Musik ist zu laut."

> „Mach eine 30-Sekunden-Version fürs Hochformat."

Jedes davon ändert ein paar Zahlen in `project.json`. Was tatsächlich neu
gerendert wird, ist genau das, was sich geändert hat.

---

## Die drei häufigsten Probleme

### 1. Die Untertitel sehen aus wie Arial

**Woran du es merkst:** die Schrift stimmt nicht, es kommt aber keine
Fehlermeldung. ffmpeg beendet sich mit Erfolg.

**Warum:** libass hat die Schrift nicht gefunden und ist stillschweigend auf die
Windows-Systemschrift zurückgefallen. Meistens, weil eine *variable* Schriftdatei
im Ordner liegt — die kann ASS gar nicht nach Schriftschnitt ansteuern.

**Lösung:** In `assets/fonts/` gehören nur statische Einzelschnitte
(`Inter-ExtraBold.ttf`, `Montserrat-Bold.ttf`, …), und der Name im Untertitel muss
exakt der Name in der Datei sein. Sag **„prüf die Untertitel-Schrift"** — die
Prüfung rendert ein Testbild und vergleicht es mit einer Arial-Kontrolle. Gleiches
Ergebnis heißt: falsche Schrift.

### 2. Die Grafik ist leer, oder alle Bilder sehen gleich aus

**Woran du es merkst:** die Einblendung ist unsichtbar oder starr, und wieder gibt
es keine Fehlermeldung.

**Warum:** Chrome hat seine *eigene* Fehlerseite fotografiert, weil der Pfad nicht
gestimmt hat — und meldet das als Erfolg, mit plausibler Dateigröße. Oder die
Vorlage animiert über die Uhr statt über `t`, dann erwischt der Screenshot einen
zufälligen Moment.

**Lösung:** Sag **„prüf die Grafiken"**. Die Prüfung erkennt die bekannte leere
Seite an ihrer Prüfsumme und meldet sie. Vorlagen dürfen niemals CSS-Animationen
benutzen, nur Rechnungen mit `t` — siehe [docs/graphics.md](docs/graphics.md).

### 3. Der Ton läuft aus dem Bild

**Woran du es merkst:** am Anfang passt es, am Ende sind Lippen und Ton
auseinander.

**Warum:** Fast immer eine falsch gerundete Ausstiegsmarke. Das Ende eines
Segments ist **exklusiv** — wird es falsch herum gerundet, wird jedes Segment ein
Bild zu lang. Bei 200 Schnitten sind das rund 6,7 Sekunden.

**Lösung:** Sag **„mach einen QC"**. Prüfung (a) vergleicht die tatsächliche
Bildanzahl der Ausgabe mit der Summe aller Segmentlängen, Prüfung (b) macht
dasselbe für die Samples. Ein Fehler dort zeigt genau, wo es auseinanderläuft.

Alle weiteren Symptome mit Ursache und Abhilfe:
[docs/troubleshooting.md](docs/troubleshooting.md).

---

## Wenn du es selbst laufen lassen willst

```powershell
cd <repo>

.\tools\ve.ps1 status ep042            # wo steht das Projekt?
.\tools\ve.ps1 cut ep042 --dry-run     # was würde geschnitten?
.\tools\ve.ps1 captions ep042          # Untertiteldatei schreiben
.\tools\ve.ps1 gfx ep042               # Grafiken rendern (Chrome, ~0,45 s/Bild)
.\tools\ve.ps1 render ep042 --draft    # --draft ist derzeit wirkungslos, siehe unten
.\tools\ve.ps1 qc ep042                # Qualitätskontrolle
.\tools\ve.ps1 export ep042 --open     # fertige Datei, im Explorer zeigen
```

`--draft` wird zwar angenommen, tut aber im Moment nichts: das Render-Modul
kennt keinen Entwurfsmodus. Die Kommandozeile sagt das jedes Mal dazu, statt die
Fahne stillschweigend zu schlucken — sonst hältst du irgendwann einen Entwurf
für das fertige Master.

Die Reihenfolge ist `captions` und `gfx` **vor** `render`. `render` ist der
einzige Schritt, der das Master schreibt: er legt die Grafiken darüber und
brennt erst danach die Untertitel ein. Wer die beiden hinterher laufen lässt,
hat ein Master, das eine Version zurückliegt.

Nimm immer `tools\ve.ps1` und nicht `python engine\ve.py` direkt. Der Wrapper
setzt eine einzige Zeile `PATH`, und genau die ist der komplette Fix dafür, dass
die Transkription die Grafikkarte findet. Alles andere reicht er unverändert
durch, inklusive Exit-Code.

`ve qc` endet mit **0** wenn alles sauber ist, **1** bei Warnungen und **2** bei
echten Fehlern — praktisch, wenn du es in ein eigenes Skript einbaust.

Der Cache liegt unter `.cache/` und wächst. Aufräumen:

```powershell
.\tools\ve.ps1 cache stats
.\tools\ve.ps1 cache gc --max-gb 80 --keep-days 21 --dry-run
```

Dabei wird nie etwas angefasst, das ein aktuelles Projekt noch braucht.
