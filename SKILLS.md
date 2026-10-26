# SKILLS.md — the real list of what to promote, dismiss, and leave pending

This file is the curated output of the candidate-lessons cleanup. It is the project's memory, not a log of the cleanup itself. For the raw evidence of each decision, see the `q` journal snapshots pulled from `brain.py --replay`.

The brain still stores everything under `candidate_lessons`, so nothing is deleted from the source of truth. This doc is the "front page" a person (or the next agent) should read instead of wading through 29 pending/littered candidates.

---

## 1. Inherited automatically next reel (promote to the house)

These became part of the rule set, not because they were first, but because we re-read them sober and decided they are authoritatively true for this project.

Where they are stored:
- Voiceover, moral card, captions, score, caption-wording, unique identity, style-consistency, editing-preserve-essence, video-playback, caption-safe-area — **craft layer rules** already live in `craft_lessons` / the craft gate.
- The scripting rules below — **promote to the script critic** so a new STORY.md fails fast on shape, length, concrete, dialogue, budget, and sameness before credit is spent.
- The reliability rules below — **promote to runtime checks** so `missing-check-*` stops being a "person has to catch it" entry in the journal and becomes machinery.
- The site-wall noise below — **do not promote; dismiss/archive**. A "recurring success 74×" is not a rule the house needs, and a "recurring error" that is really just "that site was down right then" is not a durable instruction.

### 1.1 Script critic — authoritatively true for all future reels

These are the rules that survived the re-read on evidence, not the first-impression noise. They are strong enough to be gates, not reminders.

- **HOOK — last beat is the question to the person, not the object.**
  The hook must be a question about a person (their choice, their silence, what they carry), not about a thing. Evidence re-read: the "paper boat in the gutter" hook was a thing-question; the owner's fix was to re-face it at the person.

- **SHAPE — at least 3 speaking beats in a 4-beat reel.**
  STORY.md §7: beats, not seconds. If fewer than 3 of your beats have someone speaking (or a clear silence that earns being silent), the shape is not there yet.

- **CONCRETE — one nameable physical thing survives.**
  Rule §3: the viewer could point at it after watching. A number, a date, a brand, a name, an object with a count on it. No concrete = no specific memory.

- **DIALOGUE — spoken lines fit the clip.**
  Rule §4: a ~3 s clip carries ~6–7 spoken words, not 9–10. If the line is too long for the shot, cut it in the script, not in the render. The ear catches a rushed reading before the audience does.

- **BUDGET — every line is within its time budget.**
  A line that exceeds its clip budget is the same defect as a too-fast reading — it spends a credit on something the reel cannot perform. Check line-length against clip-length in the script, not after.

- **MORAL — specific and earned, not a platitude.**
  A moral that could apply to anything is not a moral; it is a poster slogan. The reel's moral must be something this story alone could say.

- **SAMENESS — no two reels share architecture.**
  Rule §6: hook form, beat structure, turn type, and moral must be fresh. Repeating "tease → flashback → reverent pause" with the same half-covered-photo motif is the identical reel in a new sweater.

- **LENGTH — reel is 30–40 s, not 14 s stretched by hope.**
  If the script does not yet fill a reel, that is a script problem, not a "add a beat later" problem. A too-short reel is the same as a reel without a third speaking beat: the shape is not there.

### 1.2 Craft layer — already house rules under the craft gate

These are true and already enforced. Keep them as the house's non-negotiables; do not let a "recurring success" entry outrank them.

- Every reel ships a voiceover. Silent on a phone = scrolled past.
- Every reel lands a moral card.
- Every reel ships on-screen captions/headings.
- Every reel carries a background score that fits, ducked under the clips' own audio.
- Caption-wording is judgment, not decoration: the CTA must actually say the client-mandated join-us line, moral must read complete, and nothing exceeds the on-screen budget.
- Unique reel identity every reel — opener, fonts, transitions, quote, structure.
- Style consistency — one art-style anchor bound to every character; no mixing photoreal and animated across the reel.
- Editing preserves the essence — never trim an emotional beat, even a silent one.
- Video must actually play — valid to ffprobe is not the same as playing in the phone app.
- Captions clear the bottom 15% and the far-right edge.

### 1.3 Reliability — machinery to build, not just a journal entry

These are not "recall this next time"; they are the gaps the project keeps paying for. The cleanup here is to **build the check**, not just to write a candidate that says "nothing catches this."

- **PREFLIGHT STYLE-UNIFORM must catch style drift BEFORE the render is spent.**
  The project already knows this is one of its highest-value defects and still lets it bloom into a re-shoot. Make it a preflight check at generation time, not a post-render inspection.

- **GATEWAY-PREFLIGHT must detect a fully walled-out route BEFORE the render is spent.**
  The moment every route is down, the machine should not spend the next clip waiting on one. Detect "all gateways down" early and escalate / hold, not silently spend a credit on a doomed render.

- **CLIP-FAILURE must escalate cleanly BEFORE the render is stranded mid-reel.**
  One failed clip should not quietly strand the whole reel on one site while the machine exhausts requeues. Detect the single-clip failure early enough to decide: retry within reason, requeue, or escalate to a person with the actual problem text.

## 2. Dismissed / archived (do not inherit)

These were either already dismissed by the owner, or read again and found to be noise, first-impression, or not durable enough to be a house rule.

### 2.1 Already dismissed by the owner

- `reel-needs-voiceover` — **dismissed.** Already a house rule under the craft gate; the original candidate was closed out when voiceover became one of the enforced craft layers.

- `reel-needs-score` — **dismissed.** Same as above: the background-score requirement is now a craft-layer rule, not a candidate to promote a second time.

### 2.2 First-impression noise under re-read

These read as "recurring wall" candidates but did not survive the re-read. A wall that is really "that site succeeded a lot that day" is not a rule; a wall that is really "that site had a transient outage" is not a durable instruction; a wall that is "the runner blew up on a path it does not own" is a runner/host issue, not a rule for the reel.

- `wall-googlevids-success` — **DID NOT promote.** Evidence re-read: 74× success is not a rule waiting to be written. Dismiss it as noise.
- `wall-flow-success` — **DID NOT promote.** 3× success; same shape. Not a rule.
- `wall-vibes-success` — **DID NOT promote.** 10× success; same shape. Not a rule.
- `wall-krea-success` — **DID NOT promote.** 3× success; same shape. Not a rule.
- `wall-vibes-error` — **DID NOT promote.** "Recurring error" that is really "the site was flaky right then"; not durable enough to be a house rule in this shape.
- `wall-googlevids-credits` — **DID NOT promote.** Credits-out is dryness, not a learning to inherit; the brain already handles empty sites as dry, not as a rule.
- `wall-googlevids-login` — **DID NOT promote.** Login failures are transient auth moments, not a durable rule shape the house inherits as written.
- `wall-krea-selector` — **archive, do not inherit as a rule.** A real selector issue belongs in the site adapter, not in the house's rule memory.
- `wall-googlevids-selector` — **archive, do not inherit as a rule.** Same as above.
- `cmdfail-cd-users-cherish-documents-ai-video-gen-` — **archive, do not inherit as a rule.** The runner failed on the host side; that is a runner/host fix, not a rule for the reel.

### 2.3 Already dismissed in the cleanup itself

Students of the raw journal will see these clear; this is the front page saying so.

- `reel-needs-voiceover` — dismissed (already a craft rule).
- `reel-needs-score` — dismissed (already a craft rule).

## 3. Still pending by honest choice

The rest are still pending, but only because they are real next-work items, not because the cleanup failed to look at them.

- The `dadaji-ka-kahani` frame question is **not** a candidate-lesson problem to dismiss; it is the held reel's open gate and must be settled by a person on the evidence before that reel is closed. The cleanup does not pretend otherwise.
- The outage-only reels need an honest eye decision, not a candidate tweak: either you looked by eye and the reel is clear, or you did not and the reel is not closed yet.
- The unfinished reels are unfinished; that is a deliverables problem, not a rules problem.

## 4. How to read this doc without losing the evidence

- The "front page" is this file.
- The raw evidence for each promotion, dismissal, and non-promotion is in the `q` journal snapshots under `script-proof/` (and wherever the replay was stored).
- The live system still holds all candidates under `candidate_lessons`; this doc is the curated read, and the cleanup's job was to make the read short enough that someone will actually read it.

