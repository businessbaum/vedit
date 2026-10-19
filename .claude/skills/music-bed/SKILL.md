---
name: music-bed
description: This skill should be used when the user asks for background music under the dialogue - "leg Musik drunter", "mach Musik drunter", "pack einen Musikteppich drunter", "Musik leiser wenn ich rede", "duck die Musik", "Musik rein- und rausblenden", "die Musik ist zu laut", "mach den Sound fertig", "add a music bed", "put music under this", "duck the music", "sidechain the music", "the music is too loud", "fade the music in and out". Covers the music track in project.json, sidechain ducking against the dialogue, and the one-pass audio graph.
argument-hint: [project-slug] [music-file]
arguments: [project, music]
---

# music-bed

Add a music track under the dialogue with sidechain ducking, fades and correct gain staging.
Arguments: `$project` slug, `$music` a file in `assets/music/`. Full request in
`$ARGUMENTS`. An empty `$`-placeholder means the skill auto-triggered rather than being
slash-invoked: take the project from `PIPELINE_STATE` in the session context and say which
one you picked. Verb names follow the build contract; `python engine/ve.py --help` wins.

## The architecture this stage lives inside

**Dialogue audio is rendered in ONE pass over the whole timeline.** Never per segment, never
cached per segment. Concatenating separately-encoded AAC segments drifts about 10.7 ms per
junction - three 1.000 s segments joined measured 3.021333 s. Over 150 cuts that is over a
second of lip-sync error that no amount of careful cutting can recover.

The chain is `atrim -> asetpts -> afade -> adelay=<n>S -> amix=normalize=0`, into
`pcm_s24le`. There are no joins, so there is no drift. The music track is one more input into
the same `amix`.

Two spellings in that chain are load-bearing:

- **`adelay` must carry the `S` (samples) suffix.** The bare form takes integer
  milliseconds, and timeline positions derived from frames are not integer milliseconds -
  every segment would round-drift. Verified: `adelay=48001S` places the first sample at index
  48002.
- **`amix` must carry `normalize=0`.** Without it, amix divides by the input count and the
  voice silently loses about 6 dB the moment music is added. The symptom is "the dialogue got
  quieter when I added music", and it will be blamed on the ducking.

## Step 1 - pick the track and check the licence question once

Music lives in `assets/music/` and is gitignored. Ask once whether the track is cleared for
the target platform, record the answer, and do not ask again for the same file. Content ID
claims are the user's problem to decide, not this skill's to police - but silently mastering
an uncleared track into a final export is not helpful either.

Probe the file. If it is not 48 kHz it will be resampled with soxr, which is fine. If it is
shorter than the timeline, say so and ask: loop, or let it end.

## Step 2 - write the audio track

Add to `audio_tracks[]`:

```
{ "id": "music", "kind": "music", "path": "../../assets/music/<file>",
  "timeline_in_frames": 0, "gain_db": -22,
  "fade_in_s": 1.5, "fade_out_s": 3.0,
  "ducking": { "enabled": true, "sidechain": "vo",
               "threshold": 0.03, "ratio": 8, "attack_ms": 20, "release_ms": 400 } }
```

The dialogue track is `{"id": "vo", "kind": "dialogue", "source": "segments"}` and is the
sidechain key. `timeline_in_frames` is in project frames like everything else.

Why these numbers:

- **-22 dB bed.** Music under speech is felt, not heard. If the user can name the melody
  while listening to the words, it is too loud. -22 is a starting point for a modern
  talking-head mix; a cinematic cold open wants more, a tutorial wants less or none.
- **attack 20 ms.** Fast enough that the music is already down on the first syllable. Slower
  and the word gets stepped on.
- **release 400 ms.** Slow enough that the music does not pump between words. Shorter and
  every inter-word gap breathes audibly - the classic broadcast-automation sound.
- **ratio 8, threshold 0.03.** Firm ducking. Gentle ducking on a -22 bed does nothing.

**These defaults are research-derived, not measured on this user's voice.** While
`"calibrated": false`, say so when the user asks whether the level is right.

## Step 3 - fades, and where music should actually start

Fades are on the music track, not the master. 1.5 s in, 3.0 s out is the default.

Editorial notes worth offering, once:

- Music that starts with the first frame competes with the hook. Starting it 1-2 seconds in,
  under the second sentence, is almost always better.
- A bed that runs the full length of a long video stops being noticed after 90 seconds and
  then only costs headroom. For longform, propose music under the open and the close and
  silence in the middle.
- If the video has a hard structural break, the natural place to change or drop the music is
  that break, not an arbitrary time.

Offer these; do not impose them.

## Step 4 - render and listen

```
python engine/ve.py render $project
```

One pass, `pcm_s24le`, no lossy intermediate. Dialogue is never encoded to AAC before the
final mux - AAC exactly once, at export.

Then check, with numbers rather than adjectives:

- `ebur128` on the dialogue alone and on the mix. The difference tells you what the music
  actually added.
- Sample count against `frames / fps * 48000`. QC check (b). A mismatch here means the
  `adelay` suffix or a fade length is wrong.
- Listen to one ducking transition and one fade. Two spot checks catch nearly everything.

## Step 5 - loudness is set at export, not here

Do not normalise the music bed to a target. Loudness normalisation is a two-pass `loudnorm`
on the master, driven by `output.platform`, and it belongs to export-final. Normalising
twice is how a mix ends up over-compressed.

One thing worth saying now: **loudnorm pass 2 must carry an explicit `-ar 48000`** or the
output is silently 192 kHz. That is export-final's job to get right, but it is the failure
most likely to be blamed on the music stage.

Set `stage` to `"music"`.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Dialogue got ~6 dB quieter when music was added | `amix` without `normalize=0` | Add `normalize=0` |
| Sync drifts across the video | Audio concatenated per segment | One pass over the whole timeline |
| Music position off by a few ms | `adelay` in milliseconds | Use the `S` samples suffix |
| Music pumps between words | Release too short | Raise `release_ms` toward 400+ |
| First word stepped on | Attack too slow | Lower `attack_ms` toward 20 |
| Ducking inaudible | Gentle ratio on a quiet bed | Ratio 8, threshold 0.03, or raise the bed and duck harder |
| Master is 192 kHz | loudnorm pass 2 missing `-ar 48000` | Fix at export; check QC (f) |
