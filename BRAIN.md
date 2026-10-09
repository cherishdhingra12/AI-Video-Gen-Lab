# BRAIN.md — the local brain of AI Video Gen Lab

One project, one brain. This project runs on its own reflexes — perceive, remember,
learn, judge, act — without asking anything or anyone else for the day-to-day work.
Each project in the lab gets its own local brain like this; **later they all plug into
one central "mother brain."** This file is the anatomy and the plug.

## Front door
Everything talks to `brain.py` (`LocalBrain`), never to the organs directly.

```
python3 brain.py                  # self-report: health, source budget, work, what it knows/needs
python3 brain.py --json           # same, machine-readable (the mother-brain read port)
python3 brain.py review <reel>    # judgment: run the craft gate on a reel (verdict saved)
python3 brain.py reels            # every finished reel + its saved verdict: pass / held / unchecked
python3 brain.py sessions         # episodic memory: recent distilled session notes
python3 brain.py candidates       # lessons the brain PROPOSED from patterns, awaiting your call
python3 brain.py promote <slug>   # accept a candidate -> it becomes an enforced craft lesson
python3 brain.py dismiss <slug>   # reject a candidate -> frozen, never re-proposed
python3 brain.py --selfcheck      # offline proof the report + sensing + mining work
```

## Anatomy (the organs it ties together — it does not re-implement them)
| function   | organ                 | what it does |
|------------|-----------------------|--------------|
| perception | `automation/vision.py`| screenshot → vision model → page state / captcha / the account's live credit balance; also medium (`3d` vs `photoreal`) and cross-scene character comparison |
| memory     | `automation/state.py` | durable SQLite (`state.db`): jobs, credits, lessons, craft_lessons, reel_identity, reel_qc — survives a crash |
| reflex     | `automation/learn.py` | pick the best source, route around walls, escalating cooldown (anti-ban) |
| judgment   | `automation/qc.py`    | the craft gate — may a reel ship? may it go_live? Saves every verdict to `reel_qc`, so HELD stays visible in `report()` / `reels` |
| motor      | `automation/runner.py`| the loop: drive one clip end-to-end, resume mid-flight after a reboot |
| planning   | `agents.py`           | idea → script → per-clip prompts with a locked world → **art direction** (the reel's `identity.json` + its caption cards, written to avoid every prior reel's devices) |
| assembly   | `automation/assemble.py` | clips → the finished reel: dissolves, dead air trimmed off clip ends, cards placed in the empty band, the score bed — then the encode is verified to actually PLAY |
| score      | `automation/music.py` | picks the bed by **measuring** the tracks (brightness, tempo, onsets) against the reel's mood; library first, a search only when nothing fits, and the new track is filed back into the library |

Every rule the organs enforce traces back to a file on disk, never to a model's memory:
**`WAVELENGTH.md`** is the house taste (audience, language, medium, narration, captions,
identity) and every LLM call reads it at call time. Change it there and the next run obeys it.

### Which model answers (`.env` — the one thing a model swap must not lose)

Two routers, tried in order, both OpenAI-shaped (`agents.chat`). **`LLM_ORDER`** sets the order
and each router has a pinned **model**, so a swap is an `.env` edit and never a code change:

| router | `.env` | model | timeout | why |
|--------|--------|-------|---------|-----|
| FreeLLMAPI (`localhost:3001`) | `FREELLMAPI_MODEL` | `gpt-oss-120b` | `FREELLMAPI_TIMEOUT=120` | the fast one — a whole presentation in ~8s, and the only roster here that holds the JSON schema |
| OmniRoute (`localhost:20128`) | `OMNIROUTE_MODEL` | `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free` | `OMNIROUTE_TIMEOUT=90` | the fallback — ~22s, but it answers when FreeLLMAPI 429s |

**Measured 2026-10-09, against the real agent-3 task** (JSON + Devanagari captions, ≤34 chars):
`auto` on either router is a trap — it routed to `dots-studio/dots-3-note-preview:free`, which
returns prose and no braces. OmniRoute's catalogue is ~1,000 models and most of the strong ones
are unreachable on this account (`cl/*`, `agentrouter/*`, `anyapi/*` → 401/402/403); only the
`openrouter/*` namespace is live. A timeout below a model's real latency looks exactly like a
dead route — the 20s default timed out on a model that answered fine in 22s.

**Vision is pinned too (`VISION_MODEL=gemini-3.8-flash`).** The same `auto` trap bites harder here:
a router answering `auto` may pick a *different* vision model on every call, so the same unchanged
frame is classified two different ways. Measured 2026-10-09 — six identical calls on one frame of
`darzi-ka-tukda` returned `3d` ×3 and `photoreal` ×3, and the style pre-flight then rejected a
finished reel on a coin flip (the next pass rejected a different set of clips). `vision._models_for`
now sends the pinned model first and keeps `auto` only as the last resort; `vision.py --selfcheck`
asserts the pin so a later edit cannot quietly drop it.

Robustness lives in `agents.chat`, not in the model choice: a rate-limit or 5xx is retried
`LLM_RETRIES` (default 3) times before the next router is tried, and a 401/403 is not retried
at all. `_extract_json` takes the first *balanced* object, so fences and reasoning prose are
already survivable — what is not is an answer that never reaches a closing brace.

It is **self-sufficient**: it knows what work is pending, where it has free credit (each
balance is **observed** off the site on every runner pass, labelled `observed` with its read
age — a number nobody has seen on the site stays labelled `manual`), what walls it is cooling
down from, what the owner has taught it, and what it still needs from
outside (a re-login, a credit reset, an owner review). It reports all of that itself.

## One idea in, one finished reel out
The whole engine is one command, and nothing leaves it before the owner has seen it:

```
python3 pipeline.py "<the idea>"      # agents 1–3 + the score, then the clips are queued
python3 automation/runner.py          # drive the clips, then ASSEMBLE and gate the reel
python3 brain.py review <reel>        # the same craft gate, on demand
```

`pipeline.py` writes the script, the shot list, the reel's `identity.json` and its caption
cards, and puts the score on the shelf — **before** the first clip is generated, because the
identity is what the clip prompts are built to serve. The runner drives the clips and then
calls the ONE assembler (`automation/assemble.py`); when the reel is built, the craft gate
runs on it automatically and the verdict is saved. **Nothing auto-publishes.** go_live is the
owner's call, every time.

The gate that runs on every finished reel: it must PLAY (not freeze, not run silent), its text
must clear the IG-safe area, the whole reel must sit in ONE art style as its identity declares,
the recurring character must be the same person in every scene, the pacing must be usable, and
the craft layers (cards / narration / moral / score / identity) must actually exist on disk.

## What it knows (the two kinds of learning)
- **anti-ban `lessons`** — a wall (captcha/rate-limit) cools a `(site,account)` pair; the
  reflex steps back and comes back later. Escalating backoff.
- **`craft_lessons`** — what the owner taught about quality. `auto` ones a check in
  `qc.CHECKS` enforces; `judgment` ones become a checklist for human eyes. `block` /
  `publish-gate` / `warn`. Teach one: `brain.learn(slug=..., title=..., why=..., tier=..., severity=...)`.
- **`reel_identity`** — a fingerprint per shipped reel so a new one can't silently reuse an
  old look.

## How it learns on its own (two autonomous loops + an owner-vetted one)
The brain is always sensing — `observe` fires on every change **and on every tool
failure** (logged as a `*_fail` event) — and reflects at each session end
(`reflect`). On top of that:

- **Conversation self-learning (autonomous, no approval).** A `Stop` hook runs `harvest` after
  every turn: it reads the new transcript turns, asks the LLM to distill any durable **craft lesson**
  (owner taste, corrections, applied techniques), and **self-writes them to `craft_lessons`
  directly** — no human in the loop. These land as `judgment`/`warn` (advisory) on purpose, so
  autonomous learning informs every reel's checklist but can **never auto-BLOCK** a reel; the owner
  can elevate or prune. A per-session cursor means each turn only re-reads what's new.
- **Failure-pattern mining (`propose`, owner-vetted).** Reads three durable failure signals —
  recurring anti-ban walls, repeated job failures, in-session repeated failures — and files
  **candidate** lessons that stay out of the QC gate until the owner `promote`s one. (Reliability
  patterns stay gated; craft taste self-learns.)
- **Self-heal (vision).** A missed selector relocates itself via `vision.find_control` and logs the
  heal, so recurring UI drift becomes a candidate too. See [[vision-selfheal-backend]].

Craft lessons live in `craft_lessons` (what the QC gate surfaces); `reel_identity` fingerprints keep
each reel's look distinct; `reel_qc` keeps the last gate verdict per reel, so a finished-but-HELD
deliverable shows up in `report()['needs']` instead of being forgotten after a printout.

## How the hooks are wired (two front doors, one brain)
The brain has no thread into the editor — each tool calls it out of process with a JSON payload on
stdin (`{"tool_name","tool_input","session_id"}` / `{"session_id"}` / `{"transcript_path"}`).
Both supported editors are wired, so sensing never depends on which one drives the session:

| brain call | Claude Code — `.claude/settings.json` | opencode — `.opencode/plugins/brain.js` |
|------------|---------------------------------------|-----------------------------------------|
| `observe` | PostToolUse on Edit\|Write\|Bash | `message.part.updated`, tool state `completed` |
| `observe fail` | PostToolUseFailure | the same event, tool state `error` |
| `harvest` | Stop (async, after every turn) | `session.idle` → the SDK exports the session as JSONL |
| `reflect` | SessionEnd | `session.idle` debounced by a 5-min quiet period, plus a catch-up reflect for an abandoned session when the next one starts |

opencode ships no declarative hooks — its hooks are plugin callbacks — so the plugin translates tool
names (`edit`→`Edit`, `apply_patch`→`Edit`, `bash`→`Bash`) and arg keys (`filePath`→`file_path`) into
brain.py's vocabulary: **one sensing code path, both editors**. It has no session-end event, so a
session counts as over once no tool has run for 5 minutes.

Everything is fail-open — a brain that hangs, errors or is missing must never block a turn. A brain
error lands in `automation/.brain_hook.log` (capped) instead of vanishing. Concurrent hooks used to
lose events on `database is locked` (`PRAGMA journal_mode` ignores SQLite's busy timeout), so
`State.__init__` now backs off and retries: a silently dropped sensed event blinds the brain.

## Mother-brain seam (defined, NOT wired yet — on purpose)
The central brain does not exist yet, so there is no transport, daemon, or registration here
(building them now would be guessing at the other end). What *is* fixed is the contract:

- **read port** — `brain.report()` returns a plain JSON-able dict (also `brain.py --json`).
  A central brain polls this to know each project's health/budget/work/needs — including the
  finished reels and their saved gate verdicts (`reels`: pass / held / unchecked). Stable shape.
- **command port** — `brain.directive({"op": ...})`. Today it answers only read-only ops
  (`report`, `review`); anything else raises `NotImplementedError`. When the mother brain is
  real, wire a transport + auth here and widen the op set. Keep the surface tiny until then.

That is the whole plug: a local brain is "connected" when something on the other side calls
`report()` to listen and `directive()` to command. Nothing else about this project changes.
