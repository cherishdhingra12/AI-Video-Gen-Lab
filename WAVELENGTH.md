# WAVELENGTH.md — the house wavelength

**This is the brain's taste, and it is a file on disk — not a model's memory.** Every LLM call
in this project reads it at call time (`agents._wavelength()`), so swapping models, gateways or
sessions cannot lose it. Change the wavelength here and the very next run obeys it.

Standing brief: short reels about **elderly people**, for an **Indian audience**, with **a moral**
and **a viral pull**, made as **3-D animation**.

## Audience & language

Made for an **Indian audience** — the references, the humour, the family shapes and the setting
are Indian, and a line that only lands for a non-Indian viewer is the wrong line.

- **Speech is Hindi.** Every spoken line, in every clip, in Hindi.
- **On-screen text is Hindi (Devanagari) or Hinglish (Roman-script Hindi)** — whichever reads
  better for that line. Devanagari carries the hook and the moral; Hinglish suits the casual,
  comic and conversational lines. Never English-only.
- Hindi here means the Hindi people actually speak — everyday register, not literary or
  Sanskritised. Slang and a natural Hinglish mix in dialogue are correct, not flaws.

---

## 1. Medium — 3-D animation, always

Every frame of every scene is **stylized 3-D animation** in a high-end animated-feature look:
rounded shapes, soft global illumination, cinematic lighting, shallow depth of field.

Never photoreal. Never live action. Never 2-D. Never a real actor or a real room.

The style anchor is the FIRST sentence of every clip prompt and is **restated verbatim at the
end** with the negation attached — a style block that sits only at the end is how a reel once
shipped a photoreal opener in front of 3-D scenes (`agents.clip_prompts` is the fix; its
self-check is the regression test). And the medium is **stated at the very START of the build, not
discovered after a render** (owner rule 2026-10-10): `agents.write_script` and `build_shotlist`
carry the 3-D requirement and repair a non-animated style to the house anchor, and every clip
prompt OPENS with `REQUIRED OUTPUT: 3-D ANIMATED VIDEO` before the style block. Catch it at
generation time; the gate is the last resort, not the first.

Negation to attach verbatim:

> This is a computer-generated animation, NOT photorealistic, NOT live action, NOT real
> footage, NOT a 2-D drawing: no real skin pores, no camera grain of a real lens, no
> photographic realism — every frame, every clip, one single animated look.

*Every reel in this house is 3-D. This is a RULE, not the plan's choice, and it is enforced in
code three ways: `agents.clip_prompts` **replaces** a missing or non-animated style with the
house 3-D anchor (so a photoreal prompt cannot reach a site), `agents._presentation_selfcheck`
refuses an identity whose `visual_style` does not read as animation, and the craft gate holds
every new reel to this medium **no matter what its `identity.json` claims** — a photoreal
declaration is a description, never a permission (`qc._declared_medium`).*

*The three reels shipped before 2026-10-08 — `apne`, `haath_thaame`, `roz_ki_list` — are
photoreal by the owner's own design and are grandfathered; that list (`qc.PHOTOREAL_GRANDFATHERED`)
is CLOSED and no later reel may join it. `dadaji-ka-kahani` (2026-10-10) shipped photoreal when
the generator drifted and its identity was then rewritten to describe the drift — the gate now
holds exactly that case.*

## 2. Subject — old age, with a moral

The story is about an elderly person's inner life: dignity, loneliness, memory, a small
victory, a quiet generosity. The old person is the **protagonist** — never the punchline, never
an object of pity.

Every reel ends on a **moral**: one line, true, earned by the story, never a platitude. The
moral is a caption card, not spoken narration.

Viral pull is the **hook** — a curiosity gap, a number, or an image that makes no sense until
the end. Hooks that have worked: a phone secretly recording a hum; a full visitors' register; a
grandfather stuck in a bunny filter; a drawer of handwritten lists.

How the writing is actually done — the banned openings, why a list is not a story, how a
grandmother talks, the word budget per line — lives in **`STORY.md`**, read at call time by the
agents that write words and judged against by `agents.critique_script`. That file is the standard;
this section is the taste it serves.

## 3. Narration — spoken IN the scene, never added later

Dialogue belongs to the generated clip, spoken by a character on camera, in **Hindi**. Write
the line into the scene itself.

**Never** synthesize a voice after the fact: no TTS, no `say`, no narration track mixed in at
assembly. If the story needs a line, someone in the scene says it. QC enforces this: a `vo/`
folder of separately synthesized lines is a publish-gate (`no-synth-voiceover`), and every new
reel built from the craft template has no VO path at all — only the background bed is added at
assembly, under the clips' own audio.

Silent beats are allowed and often better — a card can carry a line nobody speaks.

## 4. Captions — added at assembly, in the frame's empty band

On-screen text is generated *after* the clips, in **Devanagari**. Rotate the display face —
never repeat the previous reel's font (`identity.json` records it):

Baloo 2 (rounded) · Mukta Vaani (rounded) · Rozha One (display serif) · Tiro Devanagari Hindi ·
Devanagari Sangam MN

Place each card in the **emptiest band of the frame it sits over** — sample the frames beneath
the card, find where there is no face and no detailed business, and put it there. **Never over a
face.** Never in the bottom 15% (≈288px of 1080×1920) and never at the far-right edge: the app
paints the handle, caption, audio row and action buttons there.

Faces are **found, not guessed**. Scoring "skin" by colour fails exactly where reels live: a warm
amber room makes the wall the same RGB as the face, the skin mask fires on most of the picture,
and the softest patch (the face) wins — which is how the owner came to see a title printed across
the grandfather's face. Placement asks macOS Vision (on-device, offline) where the faces are and
prices a face box so nothing can out-bid it; the colour rule survives only as a fallback for a
machine without Vision. QC re-asks the same question of the finished cut (`caption-on-face`).

## 4b. The film carries no text of its own

The generated clips must contain **no writing at all** — no words on walls, signboards, papers,
phone or TV screens, clothing or labels, and no subtitle bars or caption cards of their own. Every
card is added *after* the cut, in this caption system. A generator that is even hinted at showing
text renders **gibberish Devanagari** baked into the frame (a wall of nonsense letters; a phone
screen of wrong words), and the real card then has to sit over it and reads as a fake.

So the no-text rule is not left to a model's memory: `agents.clip_prompts` appends it to **every**
clip prompt by code (`NO_TEXT`), and the shot-list prompt forbids asking for a caption, a heading,
or a screen with text. Writing that *is* the subject of a close-up (a specific sign, a named list)
is rendered separately and composited — never requested from the generator.

## 5. One reel, one identity

No two reels share a look. Opener, transition vocabulary, caption face, structural shape, moral
and motif are fresh every time — `identity.json` is the fingerprint, and QC blocks a reel that
repeats more than 60% of a prior one.

Transitions serve the story's motif (a dissolve on a continuous hum; hard cuts on comic timing;
a match-cut on clasped hands). Speed changes and silence trimming happen at assembly — but
**never trim an emotional beat**, and never cut a silent moment short because it is silent.

### The same person from the first scene to the last

One reel has ONE cast, and the cast is locked in the prompt — not left to a model's memory.

`agents._char_lock` builds a single sentence per reel and appends it **verbatim** to every clip
prompt: the same words, in the same place, on every clip. It quotes the character's own
description back out of `world` (the sentences naming an ALL-CAPS tag, or the opening sentence
when the plan writes no tag), followed by the no-add negatives — no accessory, garment, jewellery,
make-up or feature added or removed along the way. A lock naming only the character's *tag* ("same
face, same hair") is not a lock: it gives the generator nothing to hold a face against, and a plan
with no ALL-CAPS tag got no lock at all — that is how one reel shipped **three different women
playing the same grandmother**. Positive-only descriptions also cannot stop what gets *added*: the
generator invented round glasses on one clip of a reel and on no other.

The lock is a guarantee, so it is never what gives when a prompt is too long. `agents._fit` trims
in this order: the shot shortens first, then the lead goes (the LOOK line restates the style),
then a hard cut. **The identity lock is never trimmed off the end of a prompt** — the last clip of
a long reel is exactly the clip a face drifts in.

### A flashback is not a drift

When the story itself jumps in time — a memory, a flood the grandfather once ran through — the
same character is *supposed* to look younger. A machine that asks "is this the same person, same
age?" across that jump is asking a question the story has already answered, and if it acts on the
answer it re-shoots the reel's reveal.

So the PLAN's own words decide it: a clip whose prompt declares a flashback / an earlier time
(`qc._time_jump_clips`) is left **out of the age comparison**, and the present-day clips are
judged against each other instead. Nothing is skipped silently — the exclusion is reported as an
item for a human, and the flashback's own clips are compared to *each other*, where the age is
constant and "same person?" is a fair question after all. A jump the plan forgets to name is not
caught here; the by-eye pass is what catches that one. dadaji-ki-yadein (2026-10-19) is the reel
that forced this: it blocked on its own mandated *"Flashback: Young Dadaji …"* beat and queued
four of its five clips for re-shoot.

### The shape a site reads

The words of a clip prompt are the same everywhere — one reel is one film. What `agents.clip_prompts`
changes per site is only how they are **written down**. **Vids and Flow are both Veo 3.1 and are the
waterfall's two primaries**, so they get the same labelled brief, one block per line: cast, action,
look, spoken line. Veo's own prompting pattern is a structured brief, and a label puts the cast and
the action in the same place on every clip. Every other site keeps the single flowing paragraph it
has always been given, so nothing that already renders is re-shaped. A failover moves the whole
reel to one site (one reel, one source), so the bid site's shape is what the fallback gets too.

### Camera moves: never *into* things

One gentle move per clip, from four: slow push-in, slow pull-back, pan, drift. **Never** a camera
that flies, glides, dives or travels **into** a frame, a photo, a screen or an object — the
generator obeys it literally and the shot becomes a lurching zoom. A memory shown inside a wall
photo is reached by cutting to it, or by a slow pan **across** it. The owner cut exactly such a
move (camera pushing into the bicycle frame) out of a reel.

### The gaps between lines — one span per line, never a splice

Generated clips pad the line with a beat of nothing on either side, so a 10s clip can carry 5s of
speech and ship with the other 5s spread as dead air. **The edit is one span per clip: it starts
`PRE` (0.5s) before the first line and ends `POST` (0.6s) after the last one — and nothing inside
that span is cut, however silent it measures.** What replaces the padding is a *late start*, not a
splice: a clip whose line lands at the end simply begins three seconds in.

Cutting the *middle* is what puts a seam in the picture, and a reel built that way reads as "very
abrupt back-to-back cuts" (owner, 2026-10-11) — the first version of this rule returned the
complement of every silence, 2-4 splices per clip, nine across one reel. Measured on that reel,
one of its "silent" holes is three seconds of the old woman **smiling straight at camera**: the
sound calls it dead air, the picture calls it the beat. The picture wins — a pause between two
lines is a performance beat, not padding, and the sound is not allowed to overrule it.

- The cut runs on picture and sound together, so lip-sync never moves. This is what makes a reel
  **shorter** and **crisper** rather than merely faster — and it is why speed stays a taste call
  (below) instead of the default fix for a slow reel.
- **A clip with no voice is never cut by the sound.** A wordless clip — a sunrise sweep, a silent
  beat — is left whole and reported at the end of the build, because there the picture decides and
  a human's eye settles it ("where there are no vocals, that needs to be taken care of by seeing
  the video"). Whether a clip speaks is measured by its own **dynamic range in the voice band**
  (250–3500 Hz, p95 minus the floor): the dadi reel's four spoken clips ran 26–40 dB, its wordless
  sweep 16.5. Never by level alone — the sweep's ambience bridges a −35 dB silence gate, and its
  loud fraction matches a spoken clip's.
- *Ceiling: a clip with a long, genuinely empty stretch between two lines keeps it. Nothing
  offline can tell that stretch from a held expression — the camera drifts in every clip, so
  stillness does not separate them — so the machine keeps it and the eye decides.*

- **Pace (`speed`)** — a whole-reel pace adjustment, applied to picture and sound together so
  lip-sync stays locked and the pitch holds. It is **off by default** (a reel plays at its own
  pace) because how a reel breathes is a taste call on that reel, not a house setting. Turn it on
  for one reel with `"speed": 1.06` in `identity.json` (or `SPEED=1.06` on the command line) when
  the source's motion runs slow — the Veo-family reels were craft-approved at **1.06x**; 1.12x
  read as rushed. Usable range 0.5–2.0; anything outside is ignored rather than obeyed.
- **Silence trimming** is at the **ends of a clip only** — never an internal pause, which is a
  performance beat. Some of the pause is kept so a line never starts clipped.

Per-clip speed ramps (a slow-motion hero shot inside an otherwise normal reel) are **not built**.

- **One reel, one source.** Every clip in a reel is generated on the SAME site — the reel's first
  finished clip locks it, and nothing else may add a scene to that reel. Two sites do not draw the
  same character: the face drifts, the render texture changes, and the reel stops being one world.
  A source that is cooling or out of credit is **waited out**, never worked around; a delay costs
  an hour, a mixed reel costs the consistency the reel exists for. The engine enforces this
  (`State.project_site` → `Brain.pick(strict=True)`), because no gate can see it afterwards.
  The **waterfall** picks which site a reel *starts* on: Google Vids first (one consistent
  character + native in-scene Hindi voice), then Flow, then the rest. Before the first clip lands,
  an unusable primary moves the **whole** reel to the next site (`runner.process_job` +
  `State.lock_project`); after the first clip the lock is absolute.

## 6. When a review cannot run — the by-eye fallback

The craft gate's vision checks (style-uniform, character-consistency, visual-realism) all share
the one vision gateway, and the QC frame-review exhausts it — so the gateway **does** go down.
A reel the gate could **not measure** is not a defective reel and is not a passed one. Nor is a
reel a judge merely *has an opinion* about — same rule, same answer.

- The gate routes "could not run" to the **publish-gate** (never to block): the reel may ship,
  but it does **not** go_live until a human has looked at it. A **judge's objection** (the caption
  wording judge) is the same kind of item — a model's opinion of Hindi never stops a ship on its
  own, it asks for the owner's eyes. Neither is a defect, neither is a pass, and both are settled
  the same way: a human looks and signs. Do not "fix" a judge by re-rolling it until it agrees —
  it is non-deterministic, so that is a ratchet with no end.
- That look is **by eye**: sample frames across the whole reel *and* one contact sheet — one panel
  per scene — to judge whether the recurring character is the same person and the art style holds.
  (A single frame cannot answer "same person in every panel?"; the sheet is the question.)
- **The by-eye review is the agent's job, not a ticket for the owner.** When the gateway is down,
  the agent samples the frames, *looks at them*, answers each stalled check itself — style-uniform,
  character-consistency, visual-realism — and **shows the owner the verdict with the frames it was
  drawn from**. An outage is never returned as "waiting for your eyes": saying "I could not run the
  check" while the pictures are sitting right there is handing back a job the agent can do. Owner
  rule 2026-10-12, after exactly that was handed back to him. The sign-off below still gates
  go_live — the agent decides whether the reel is *defective*, the owner decides whether it ships.
- The gate writes that look for you: `eye_review_sheet.png`, two full-size frames and
  `eye_review.json` (every item that needs a human's eyes) land in the project dir. The human
  looks, then
  `python3 automation/qc.py projects/<name> --signed-by '<name>' [--note '...']` records the
  sign-off and re-runs the gate. The signature is on the **cut** (`qc.Reel.fp`), not the project —
  rebuild the reel and the sign-off is void — and no sign-off ever clears a real defect.
- Never let an outage read as a craft failure, and never let it read as a pass. Say which it was.

## 7. The score — audible from the first second

A reel ships with a background bed, and the bed must be **audible through the whole reel**, not
merely present on disk. The failure that cost a reel (darzi-ka-tukda, 2026-10-10): the bed *was*
mixed in, but Indian classical tracks open on a slow **alap** — ~16s of near-silence — and the mix
started at t=0, so the reel's entire hook and first scenes played with no music. Every offline
check was green; only listening caught it.

- Placement is **measured**, never assumed: `music.analyse` reports `level_start` (where the track
  first reaches its own body) and the assembler skips the track's quiet intro rather than playing it.
- Level is **normalised**, not fixed: a quietly-mastered bed is lifted toward its target instead of
  landing doubly quiet under one hard-coded multiplier.
- **Presence on disk is not presence in the mix.** QC proves the score reached the audio by its
  *floor* — the quietest second of the finished reel. A bed carrying the reel keeps the floor within
  ~10 dB of the narration; a missing or silent-intro bed drops it far below anything the reel says.
  Measured across the fleet: healthy reels floor at −29…−41 dB, the silent-intro bed at −50,
  no bed at −56. Do not score this from a file's existence — and never with `silencedetect`, which
  reads a quiet alap as "not silent" and passes the defect.

## 8. Brand

`standardsunrisefoundation_ngo` — Standard Sunrise Foundation, **standardsunrisefoundation.org**
(`agents.BRAND_URL`; the caption self-check asserts the two agree). The closing CTA is a card in
the same caption system, and it lands as the last thing on screen — carrying that address and no
other. A wrong domain is a fact the model got wrong, not a taste call, so the code rewrites any
web address on a card to this one before the card is written (`agents._fix_url`).

## 9. The reel's folder — everything in one place

**The folder is the deliverable.** A reel is not finished when the cut plays; it is finished when
one folder holds everything needed to review it, approve it and post it, with nothing left in a
terminal or in someone's head. The owner's rule (2026-10-15), holding up `projects/dadaji-ka-kahani`
as the template: *"right from the music to the caption, to the publishing, to the shotlist, to the
script, to the covers — everything should be in this folder, just like this."*

The list is one definition, `publishing.FOLDER`, beside the code that writes its members:

| file | who writes it |
|---|---|
| `script.md` | `agents.write_script` |
| `shotlist.json` | `agents.build_shotlist` |
| `identity.json` | `agents.presentation` |
| `captions.json` | `agents.presentation` |
| `music_raw.mp3` | `music.choose` |
| `music_meta.json` | `music.install` (where the score came from + how it is placed) |
| `final_veo.mp4` | `assemble.build` |
| `caption_place.json` | `assemble.build` (where each card landed) |
| `music_bed.json` | `assemble.build` (the measured floor of the shipped cut) |
| `cover.png` | `publishing.cover` |
| `publishing.md` | `publishing.sheet` |

- Two of these were previously made **by hand**, which is why the folder could be complete one reel
  and not the next. Both are now written by the build (`assemble.build` calls `publishing.pack`),
  so the next reel has them because the pipeline wrote them, not because someone remembered.
- A missing member is a **publish-gate**, never a block: the reel may be perfect, it just cannot be
  posted without going back to the terminal. `qc.check_reel_folder` names every missing file and the
  command that writes them (`publishing.py <dir>`).
- **The cover is a real frame of the reel**, never a generated image — it is the one picture the reel
  is picked by, and a cover that is not in the film is a promise the film does not keep. Its hook
  line sits in the **middle half of the frame**: the profile grid shows a 1:1 **centre crop**, so a
  line near the top (where the reel itself puts it) is cropped away there. The frame is taken at a
  moment `caption_place.json` says is card-free, so the cover never carries a second card.
- `publishing.md` is written **once** and never overwritten without `--force`: the owner edits it,
  and a rebuild must not undo that. An outage is not a defect here either — the sheet falls back to
  the reel's own words rather than leaving the folder incomplete.

---

## Hard nevers

1. Photoreal, live-action or 2-D frames in a new reel.
2. Narration synthesized after the clips.
3. A moral that is a platitude — or a reel with no moral at all.
4. Text over a face, in the bottom 15%, or at the right edge.
5. Repeating a prior reel's font, opener, transition or structure.
6. Cutting an emotional or silent beat for pace.
7. Mixing generation sources inside one reel (a scene from a second site).
8. Scoring a check that could **not run** (a down gateway) as a craft failure — or as a pass.
   An outage gates go_live and asks for a human's eyes; it never blocks and never clears.
