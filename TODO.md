# TODO.md — reaching 100% on the project's own definitions

This is the live todo. It is truth-anchored: every item maps to a concrete thing in the project's own records (`state.db`, `brain.py`, craft gate, `reel_qc`, `eye_review`, `SKILLS.md`, `WAVELENGTH.md`, `STORY.md`, or the transcript journal under `script-proof/`).

## 0. Done — the honest cleanup

- [x] Read every pending candidate with its full evidence
- [x] Re-read them sober, not as first-impression noise
- [x] Strip the house's own 100% conversation out of the cross-reel evidence (the "3 closed, 11 open" QC truth is the house's tripwire, not an agent's slogan)
- [x] Write the curated rules doc (`SKILLS.md`) so the next agent reads a front page instead of 29 pending candidates
- [x] Write this todo

## 1. Close the batch in the project's own records

The QC table says 3 reels are `ok:true`: `aawaaz`, `filter_wala`, `the-lamp-s-silent-promise`. The other 11 are `ok:false` and are not open for the same reason. "All reels closed" is not a statement here; it is a thing the project can re-read.

Before any reel is marked closed by hand, split the 11 open reels into honest buckets:

- **Open, unfinished craft (missing deliverables):** `roz_ki_list`, `the-ascending-lantern`, `the-lunar-lantern`, `the-starlight-fox`
  - `roz_ki_list` — `reel_qc` says no voiceover + not one art style
  - `the-ascending-lantern`, `the-lunar-lantern`, `the-starlight-fox` — no caption cards, no VO, no moral card, no score
- **Open, real defect still on the wall:** `dadaji-ka-kahani`, `dadaji-ki-yadein`, `dadaji-ki-dhimi-chal`, `dadi-ki-bucket-list`, `good-morning-dadaji`
  - Each has a real block or a real non-conviction gate in `reel_qc`, not just "missing files"
- **Open, outage-only:** `dadi-ki-dhansuchi`, `darzi-ka-tukda`
  - Held because the vision/LLM gateway could not run, not because the reel is necessarily defective

Then, reel by reel, decide which you are closing now and which you are setting aside:

- **Close by test:** the 3 already-ok reels — write a clean close-state note into `brain_log` so "all reels closed" is a readable project event, not a statement in chat.
- **Close by decision where a decision is actually needed first:** `dadaji-ka-kahani`'s frame question (style is already a block, character is a non-conviction gate — settle what "same person" means there before calling it closed).
- **Set aside honestly** where the reel is genuinely unfinished or genuinely not yet eye-reviewed.

Associated work:
- [ ] Write a short `brain_log` close-state note for the 3 ok reels
- [ ] Decide, reel by reel, which of the 11 open reels you are actually closing now vs setting aside
- [ ] Record the dadaji-ka-kahani "same person" decision shape before calling that reel closed

## 2. Put every held reel into one of three honest buckets in the QC record

"Closed" is not just a statement; it is a state the project can re-read. For each open reel, put it into one of:

- **Finished and clear by eye** — you looked and the reel is good
- **Finished with a real defect still on the wall** — you saw the defect and it is still there
- **Unfinished, missing deliverable** — the reel is not done yet

This is the honest answer to "are all reels closed?" — the QC table already stores more than enough of this; the job is to make it unambiguous.

- [ ] Read each held reel's current `reel_qc` verdict and put it into the right honest bucket
- [ ] Where a reel is outage-only, record whether you actually looked by eye (close) or did not (not closed)
- [ ] Make "closed" mean something re-readable, not just "no open blockers in this chat"

## 3. Settle the dadaji-ka-kahani frame question by decision

This is the one reel where the house's current rule and its current failure mode meet exactly: clip 2 is in a different look from the rest (style block already enforced), and the character check can't settle whether the recurring person is actually the same person across scenes (character side is a non-conviction gate).

Before calling this reel closed:
- [ ] Look at the frames for the recurring person across scenes
- [ ] Decide what "same person" means here in concrete terms (same face and hair, or the younger version in the flashback counts as the same person, or clip 2 is genuinely a different character)
- [ ] Record that decision shape, not just "closed"
- [ ] Decide whether the reel ships with that decision, or whether the style-blocked clip 2 still keeps it from shipping

## 4. Prune the learned-rules noise toward a small inherited core

The source of truth (`candidate_lessons`) still holds everything. This task is to make the read short enough that somebody actually reads it.

The curated front page is `SKILLS.md`. The cleanup decisions in it are:

**Promote to house rules (already done in the doc):**
- Craft-layer enforcement: voiceover, moral card, captions, score, caption-wording, unique identity, style-consistency, editing-preserve-essence, video-playback, caption-safe-area
- Script critic: hook, shape, concrete, dialogue, budget, moral, sameness, length
- Reliability: preflight style-uniform, gateway preflight, clip-failure escalation

**Already dismissed:**
- `reel-needs-voiceover` — already a craft rule
- `reel-needs-score` — already a craft rule

**Did not promote (noise under re-read):**
- `wall-googlevids-success`, `wall-flow-success`, `wall-vibes-success`, `wall-krea-success` — recurring success is not a rule
- `wall-vibes-error` — transient; not durable
- `wall-googlevids-credits` — dryness, already handled by the brain
- `wall-googlevids-login` — transient auth; not a durable rule shape
- `wall-krea-selector`, `wall-googlevids-selector` — belong in the site adapter, not the house rule memory
- `cmdfail-cd-users-cherish-documents-ai-video-gen-` — runner/host issue, not a reel rule

- [ ] Confirm `SKILLS.md` matches the decisions above exactly
- [ ] Decide, for the did-not-promote items, whether they should be archived or left to time out naturally in `candidate_lessons`
- [ ] Keep the brain's read port short enough to actually be read

## 5. Close the reliability gap the candidate table keeps pointing at

The three highest-hit pending reliability candidates are:

- `missing-check-clip-failure-escalation` — 62
- `missing-check-preflight-style-uniform` — 45
- `missing-check-gateway-preflight` — 1

These are not "remember this next time"; they are "the machine keeps spending a render and then asking a person to catch a problem it could have caught earlier." The work is to build the early catch, not to write another journal entry about it.

Start from the highest-hit and work down:

- [ ] Build an early style-uniform preflight so the render is not spent and then inspected
- [ ] Build a clean clip-failure escalation path so one failed clip does not quietly strand the whole reel
- [ ] Build a cleaner gateway-preflight so a fully walled-out route does not spend a render silently

Note: the exact implementation shape is a design decision for the person/agent doing this item. The requirement is the behavior, not a particular file shape.

## 6. Make the next reel the real virality test

The next reel should prove the improvements, not just receive them. That means:

- **Enforce STORY.md shape BEFORE credit is spent.** A reel that fails shape, length, concrete, dialogue, budget, moral, or sameness still spending a credit is the same defect pattern as the old reels.
- **Write the "same person" decision per reel BEFORE generation**, so the character-consistency gate is a conviction or a real documented non-conviction, not a mysterious "could not review."
- **Generate the post from the reel identity**, not as a separate judgment each time. The reel's opener, fonts, transitions, quote, structure, and moral are already stored in `reel_identity`; the post artifact (title / caption / description / hashtags) should flow from that identity.

- [ ] Enforce STORY.md shape before credit spend on the next reel
- [ ] Decide and record the per-reel "same person" frame before generation
- [ ] Generate one identity-driven posting artifact from the reel, not a separate judgment each time

## 7. Verify 100% on the project's own definitions

The project has its own definition of "100%": `brain.py --selfcheck`, the craft/gate passing cleanly on a finished reel, and a coherent credits/rotation story for the next production reel.

- [ ] Re-run `brain.py --selfcheck` after the cleanup
- [ ] Confirm the gate still passes cleanly on a finished reel (use a reel that already passed, or a genuinely finished new one)
- [ ] Confirm the credits/rotation story for the next production reel is coherent — which site the next reel starts on, whether it can afford the whole reel there, and what happens if it cannot

---

This todo is the honest plan. If you want the work done in a different order, or want me to stop at a particular item, say so — this list is scoped to what "reach 100% of what we envision" actually means in the project's own records.
