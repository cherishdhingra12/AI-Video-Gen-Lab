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
python3 brain.py review <reel>    # judgment: run the craft gate on a reel
python3 brain.py sessions         # episodic memory: recent distilled session notes
python3 brain.py candidates       # lessons the brain PROPOSED from patterns, awaiting your call
python3 brain.py promote <slug>   # accept a candidate -> it becomes an enforced craft lesson
python3 brain.py dismiss <slug>   # reject a candidate -> frozen, never re-proposed
python3 brain.py --selfcheck      # offline proof the report + sensing + mining work
```

## Anatomy (the organs it ties together — it does not re-implement them)
| function   | organ                 | what it does |
|------------|-----------------------|--------------|
| perception | `automation/vision.py`| screenshot → vision model → page state / captcha |
| memory     | `automation/state.py` | durable SQLite (`state.db`): jobs, credits, lessons, craft_lessons, reel_identity — survives a crash |
| reflex     | `automation/learn.py` | pick the best source, route around walls, escalating cooldown (anti-ban) |
| judgment   | `automation/qc.py`    | the craft gate — may a reel ship? may it go_live? |
| motor      | `automation/runner.py`| the loop: drive one clip end-to-end, resume mid-flight after a reboot |
| planning   | `automation/agents.py`| idea → script → per-clip prompts with a locked world |

It is **self-sufficient**: it knows what work is pending, where it has free credit, what
walls it is cooling down from, what the owner has taught it, and what it still needs from
outside (a re-login, a credit reset, an owner review). It reports all of that itself.

## What it knows (the two kinds of learning)
- **anti-ban `lessons`** — a wall (captcha/rate-limit) cools a `(site,account)` pair; the
  reflex steps back and comes back later. Escalating backoff.
- **`craft_lessons`** — what the owner taught about quality. `auto` ones a check in
  `qc.CHECKS` enforces; `judgment` ones become a checklist for human eyes. `block` /
  `publish-gate` / `warn`. Teach one: `brain.learn(slug=..., title=..., why=..., tier=..., severity=...)`.
- **`reel_identity`** — a fingerprint per shipped reel so a new one can't silently reuse an
  old look.

## How it learns on its own (two autonomous loops + an owner-vetted one)
The brain is always sensing — `observe` fires on every change (PostToolUse) **and on every tool
failure** (PostToolUseFailure, logged as a `*_fail` event) — and reflects at each session end
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
each reel's look distinct.

## Mother-brain seam (defined, NOT wired yet — on purpose)
The central brain does not exist yet, so there is no transport, daemon, or registration here
(building them now would be guessing at the other end). What *is* fixed is the contract:

- **read port** — `brain.report()` returns a plain JSON-able dict (also `brain.py --json`).
  A central brain polls this to know each project's health/budget/work/needs. Stable shape.
- **command port** — `brain.directive({"op": ...})`. Today it answers only read-only ops
  (`report`, `review`); anything else raises `NotImplementedError`. When the mother brain is
  real, wire a transport + auth here and widen the op set. Keep the surface tiny until then.

That is the whole plug: a local brain is "connected" when something on the other side calls
`report()` to listen and `directive()` to command. Nothing else about this project changes.
