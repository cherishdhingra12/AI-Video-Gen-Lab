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
self-check is the regression test).

Negation to attach verbatim:

> This is a computer-generated animation, NOT photorealistic, NOT live action, NOT real
> footage, NOT a 2-D drawing: no real skin pores, no camera grain of a real lens, no
> photographic realism — every frame, every clip, one single animated look.

*Reels shipped before 2026-10-08 — `apne`, `haath_thaame`, `roz_ki_list` — are photorealistic by
design and stay as they are. Each reel's `identity.json` declares its own medium and is checked
against that declaration; a reel that declares nothing gets this default.*

## 2. Subject — old age, with a moral

The story is about an elderly person's inner life: dignity, loneliness, memory, a small
victory, a quiet generosity. The old person is the **protagonist** — never the punchline, never
an object of pity.

Every reel ends on a **moral**: one line, true, earned by the story, never a platitude. The
moral is a caption card, not spoken narration.

Viral pull is the **hook** — a curiosity gap, a number, or an image that makes no sense until
the end. Hooks that have worked: a phone secretly recording a hum; a full visitors' register; a
grandfather stuck in a bunny filter; a drawer of handwritten lists.

## 3. Narration — spoken IN the scene, never added later

Dialogue belongs to the generated clip, spoken by a character on camera, in **Hindi**. Write
the line into the scene itself.

**Never** synthesize a voice after the fact: no TTS, no `say`, no narration track mixed in at
assembly. If the story needs a line, someone in the scene says it.

Silent beats are allowed and often better — a card can carry a line nobody speaks.

## 4. Captions — added at assembly, in the frame's empty band

On-screen text is generated *after* the clips, in **Devanagari**. Rotate the display face —
never repeat the previous reel's font (`identity.json` records it):

Baloo 2 (rounded) · Mukta Vaani (rounded) · Rozha One (display serif) · Tiro Devanagari Hindi ·
Devanagari Sangam MN

Place each card in the **emptiest band of the frame it sits over** — sample the frames beneath
the card, find where there is no face and no detailed business, and put it there. Never over a
face. Never in the bottom 15% (≈288px of 1080×1920) and never at the far-right edge: the app
paints the handle, caption, audio row and action buttons there.

## 5. One reel, one identity

No two reels share a look. Opener, transition vocabulary, caption face, structural shape, moral
and motif are fresh every time — `identity.json` is the fingerprint, and QC blocks a reel that
repeats more than 60% of a prior one.

Transitions serve the story's motif (a dissolve on a continuous hum; hard cuts on comic timing;
a match-cut on clasped hands). Speed changes and silence trimming happen at assembly — but
**never trim an emotional beat**, and never cut a silent moment short because it is silent.

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

## 6. When a review cannot run — the by-eye fallback

The craft gate's vision checks (style-uniform, character-consistency, visual-realism) all share
the one vision gateway, and the QC frame-review exhausts it — so the gateway **does** go down.
A reel the gate could **not measure** is not a defective reel and is not a passed one.

- The gate routes "could not run" to the **publish-gate** (never to block): the reel may ship,
  but it does **not** go_live until a human has looked at it.
- That look is **by eye**: sample frames across the whole reel *and* one contact sheet — one panel
  per scene — to judge whether the recurring character is the same person and the art style holds.
  (A single frame cannot answer "same person in every panel?"; the sheet is the question.)
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

`standardsunrisefoundation_ngo` — Standard Sunrise Foundation. The closing CTA is a card in the
same caption system, and it lands as the last thing on screen.

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
