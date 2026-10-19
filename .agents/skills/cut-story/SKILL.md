---
name: cut-story
description: This skill should be used when the user asks to decide what the video is actually about before any mechanical cutting - "bau die Story", "was soll ins Video", "was kann raus", "welche Themen bleiben", "kürz das auf 6 Minuten", "mach daraus ein kurzes Video", "wo ist der Hook", "sortier die Themen", "worum geht es hier eigentlich", "build the story", "decide what stays", "structure this", "cut this down to N minutes", "find the hook", "reorder the topics". This is the CONTENT-selection pass that writes the "story" block in project.json; it runs after transcribe and before cut-rough.
argument-hint: [project-slug] [target-minutes]
arguments: [project, target]
---

# cut-story

Decide what the video **is**. Mechanical cutting only tightens what was said in the order it
was said; this pass decides which topic blocks survive, in what order, and where the hook is.
It is the only stage that can turn a 25-minute ramble into a 6-minute video, and it is the
stage that most determines whether anyone watches.

Arguments: `$project` is the project slug, `$target` an optional target length in minutes.
Full request text is in `$ARGUMENTS`. If no slug is given, use the project named in
`PIPELINE_STATE` from the session hook and say which one you picked.

Verb names below follow the build contract. If `python engine/ve.py --help` disagrees, the
CLI wins - use its names and mention the mismatch.

## Preconditions

- `projects/$project/project.json` exists with `stage` at `transcribed` or later.
- `transcript.path` resolves and `transcript.word_count` is set.
- If `stage` is still `intake`, stop and run the transcribe skill first.

Never run this pass twice silently. If `story.blocks` already exists, show the current
decisions first and ask whether to revise or replace.

## Step 1 - get a readable transcript

Word-level JSON is unreadable prose. Flatten it first:

```
python engine/ve.py transcript flatten $project --numbered
```

This emits the words as running text with a word index marker every 25 words. Word indices
are the anchors the `story` block stores - never store seconds, never store timecodes.

## Step 2 - dispatch transcript-analyst when the transcript is long

If `word_count > 2500` (roughly 15 minutes of German speech), dispatch the
`transcript-analyst` agent instead of reading the transcript inline.

Why: a 5,000-word transcript consumes the context you need for the rest of the session, and
the analyst returns roughly 40 lines. Reading it yourself also biases you - having just read
every sentence, everything feels load-bearing. A fresh context cuts harder, which is correct.

Give the agent: the flattened transcript path, the target length, the platform from
`output.platform`, and any steer the user gave in `$ARGUMENTS`. It returns candidate blocks
with decisions and rationales. **You still own the final call** - review its drops, and
overrule any drop the user has explicitly asked to keep.

Below 2500 words, read the transcript yourself.

## Step 3 - name the video in one sentence

Before any block decision, write one German sentence that says what the viewer believes at
the end that they did not believe at the start. Show it to the user.

If that sentence cannot be written, the material does not contain a video yet. Say so plainly
and stop - recommend a pickup recording rather than assembling something shapeless. This is a
real outcome and delivering it honestly is more useful than a tidy 6-minute nothing.

Every later decision answers to that sentence.

## Step 4 - block the transcript

A block is a unit of meaning that survives being moved: 30 seconds to 3 minutes, one point.
Blocks are contiguous word ranges that tile the transcript without gaps or overlap.

Do not cut blocks at pauses. Cut them where the subject changes - the speaker's own
signposts are reliable in German: *also, jetzt zum Thema, das andere ist, kurz noch, um
nochmal zurückzukommen, was viele nicht wissen*.

## Step 5 - decide, with a bias toward dropping

Default expectation: **drop 40-60% of the recorded words.** If more than 70% of the material
survives, no decision has been made - go back and apply the drop tests harder.

Drop tests, applied in order:

1. **The belief test.** Remove the block. Does what the viewer believes at the end change? If
   not, drop it. This is the primary test; the rest are shortcuts to it.
2. **The second-example test.** Two examples of the same point: keep the more concrete one,
   drop the other. Speakers repeat themselves to think, not to teach.
3. **The orphan test.** Setup for a payoff that got dropped is now noise. Drop it, and
   re-check after every other drop - orphans appear in cascades.
4. **The warm-up test.** The first 60-90 seconds of almost any recording is throat-clearing.
   Drop it by default and tell the user you did. This single rule improves more videos than
   every filler-word removal combined.
5. **The recap test.** Spoken summaries of what was just said ("also nochmal kurz
   zusammengefasst") are a speaking crutch, not a structure. Drop unless the video is
   genuinely instructional and long.

Keep signals, strongest first: a number, a name, a date, a specific mistake the speaker
admits, a concrete worked example. **Specificity is the strongest keep signal there is.**
Abstract framing is cheap and can be re-recorded in ten seconds; a specific anecdote cannot.

Use `"decision": "maybe"` for blocks whose fate depends on the final length. Keep `maybe`
under 20% of blocks - beyond that it is indecision, not nuance, and the user has to do the
work you were asked to do.

If `$target` is set, sanity-check it: sum the kept blocks' word counts and divide by the
speaker's measured rate (`word_count` / source duration in minutes). Report the estimate
before writing anything. German talking-head speech runs ~130-160 wpm.

## Step 6 - find the hook

The hook is almost never at the start of the recording. People arrive at their real point
late, after they have warmed up and stopped performing.

**Search the last third first.** Look for: a claim that sounds wrong until explained, a
number that is bigger or smaller than expected, an admitted failure, a stake ("das hat mich
zwei Wochen gekostet"). Pick 15-25 seconds - one or two sentences, not a paragraph.

Write it as `story.hook` with a `word_range` and a German `note` saying why it was moved.
Moving the hook forward is the one reorder that is almost always right.

## Step 7 - order, and only reorder when you can name the reason

There are exactly two legitimate reasons to reorder:

1. The hook goes to the front.
2. A block answers a question a later block asks, so it must precede it.

Everything else keeps recording order. Reordering spoken material breaks backward references,
and **you will not hear the break until the render** - by which point three stages have run.

**Reference-integrity check, mandatory for every moved block.** Re-read the moved block's
first two sentences for backward references. In German these break silently: *also, deswegen,
deshalb, dadurch, dabei, wie gesagt, das, dieser/diese/dieses, außerdem, trotzdem,
andererseits, wie eben erwähnt*. If a moved block opens with one, either start the block one
sentence later, or mark it `maybe` and tell the user it needs a pickup line. Do not move it
and hope.

## Step 8 - write the story block

Write `story.blocks[]` and `story.hook` into `projects/$project/project.json` exactly as the
schema in `docs/edl-schema.md` defines: `id`, `word_range` (half-open, in word indices),
`label` (German), `decision` (`keep|drop|maybe`), `order` (output ordering, may differ from
recording order), `rationale`.

Rules that are not negotiable:

- **Nothing is deleted.** A dropped block stays in the array with `"decision": "drop"`. Every
  decision must be reversible by flipping one field. The user will disagree with some of your
  drops and must be able to undo them without re-running the pass.
- **`rationale` is one German clause and says WHY, not WHAT.** "trägt das kernargument" is
  useful; "spricht über Frame-Snapping" is a label, not a reason.
- **`order` is dense over kept blocks** starting at 0. Dropped blocks get their recording-order
  index; they are inert but must not collide.
- Set `stage` to `"story"`. Do not touch `segments[]` - that is cut-rough's territory.

Then show the user a compact table: order, label, decision, estimated minutes. That table is
the deliverable, not the JSON.

## Step 9 - hand off

Say explicitly that mechanical cutting will now run **inside the kept blocks only**, which is
both correct and cheaper - filler and retake detection over material that is about to be
dropped is wasted work. Then offer the cut-rough skill.

## Failure modes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Everything looks essential | Read the transcript inline when it was long | Dispatch transcript-analyst; a fresh context cuts harder |
| Kept 85% of the material | No editorial decision was made | Re-apply the belief test to every block |
| Video jumps oddly after reorder | Backward reference broken | Run the Step 7 check on every moved block |
| `word_range` gaps or overlaps | Blocks not tiling | Blocks must be contiguous and half-open |
| User rejects most drops | Wrong sentence in Step 3 | Rewrite the one-sentence thesis with the user, then redo |
