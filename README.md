# AI Video Gen Lab — v2 (autonomous browser automation)

Goal: frontier-quality video, ~free, automated. The Mac drives the free tiers of
frontier video sites through a real browser — sees, acts, learns from walls, and
resumes across reboots. Replaces v1 (self-hosted Wan on Modal: hazy + costly).

## How it works
1. `pipeline.py "idea"` — agents 1–3 (local LLM) write a script, a shot list of standalone
   per-clip prompts sharing one locked **world** (consistency anchor), and the reel's **art
   direction**: its `identity.json` (opener, type, quote, structure) and its caption cards.
   The score is chosen here too, from the reel's own mood. Each clip becomes a row in
   `state.db`.
2. `automation/runner.py` — the loop. Picks the best site+account with credit that
   isn't cooling down, drives one clip (Playwright, human-like), polls (selectors
   first, vision if unsure), downloads the clip. A wall (captcha/rate-limit) records
   a lesson and cools that site with escalating backoff — step back, come back later.
3. When every clip of a project is done → `automation/assemble.py` builds the finished
   reel at `projects/<slug>/final_veo.mp4`: dissolves, dead air trimmed off each clip's
   ends, the cards placed in the frame's empty band, the score bed under the clips' own
   audio — and the encode verified to actually PLAY. Then the craft gate runs on it.

## Sources — Google Vids primary, the rest as overflow
Every clip is queued against **Google Vids** (`pipeline.py` sets `PRIMARY_SITE`). One model
means one look, which is the only way "the same character in every scene" actually holds —
and it is where the cheap credits and the native in-scene Hindi voice are. The other adapters
(Kling, Hailuo, Vidu, Flow) stay wired as a *fallback*: `Brain.pick` floats Vids to the front
and reroutes only when Vids is cooling down or out of credit. It is credit-sorted, not
quality-ordered — there is no quality ranking between sites.

Multiple email accounts per site multiply the daily free ceiling. Credit counts: **verify at
signup** (`login.py --credits N` stores that first number as `manual`) — and before any reel
chooses its source, the runner's balance sweep asks every choosable account's site what it
ACTUALLY holds and stores the answer as `observed`, so the choice is made on the site's own
numbers, not signup guesses (`runner.py --sweep` runs it standalone). Nothing about credits
is hardcoded.

The house rules live in **`WAVELENGTH.md`** — medium, subject, narration, captions, identity.
`agents.py` reads that file on every LLM call, so it survives a model or gateway swap.

## First-run setup
    pip install -r requirements.txt && python3 -m playwright install chromium
    # ffmpeg/ffprobe + node must be on PATH too. Use an interpreter that HAS the deps:
    #   python3 -c "import playwright, PIL, numpy, browser_cookie3"   # must succeed
    # (on this Mac that is /usr/local/bin/python3; /usr/bin/python3 has none of them)
    # one-time manual login per site+account (cookies persist forever):
    python3 automation/login.py kling a1 --credits 60
    python3 automation/login.py googlevids personal --credits 50
    # ... repeat per site / per email account

    python3 pipeline.py "a lantern drifting through a misty bamboo forest at night"
    python3 automation/runner.py --headed     # watch it; drop --headed for silent

## Run it when you need a reel (no daemon — you start it)
Nothing runs on its own: there is deliberately NO launchd agent, no background loop.
Ask for a reel, run `pipeline.py` + `runner.py`, and the loop drains the queue,
assembles `projects/<slug>/final_veo.mp4`, then runs the craft gate over it.

Crash-safety lives in SQLite, not in a scheduler: every state change commits before
the next step, so a shutdown mid-render leaves a durable row the next run picks up
(`automation/runner.py --selfcheck` proves resume + wall/requeue + stitch offline).

    python3 brain.py                 # health, credits, work, reels + gate verdicts
    python3 brain.py reels            # every finished reel: PASS / HELD / not gated yet
    python3 brain.py review <project> # (re)run the gate — the verdict is SAVED to state.db
A HELD reel stays visible until you fix and re-gate it: the verdict lives in the
`reel_qc` table, not in a log line that scrolls away.

    ./check.sh                       # every offline self-check (no browser, no gateway)
    ./check.sh --with-network        # + the live LLM / vision round-trips

## Modules (each has a runnable self-check: `python3 <file>`)
- `automation/state.py`   — SQLite brain: jobs (resumable), credits, lessons (anti-ban)
- `automation/browser.py` — Playwright persistent context, stealth, human mouse/typing
- `automation/vision.py`  — screenshot → vision model (gemini-2.5-flash) → state/captcha/credits
- `automation/learn.py`   — consult lessons, escalating cooldown, route around walls
- `automation/runner.py`  — the loop (`--selfcheck` runs it offline with a fake site)
- `automation/sites/*.py` — one tiny adapter per site (submit/poll/download)
- `stitch.py`             — ffmpeg xfade join (carries each clip's ORIGINAL audio) + last-frame extract
- `agents.py`             — script + per-clip prompts with the world anchor + art direction
- `automation/caption.py`  — finds the empty band over the frames, renders the Hindi/Hinglish card
- `automation/assemble.py` — clips → finished reel: dissolves, de-logo, placed cards, score, verify
- `automation/music.py`    — measures the shelf, fits a bed to the reel's mood, searches + archives
- `stitch.py`             — plain xfade join, kept as the standalone 9:16 normalize utility (`assemble.py` supersedes it as the reel builder)
- `brain.py`              — the front door: self-report, `reels`, `review`, the learning loops
- `./check.sh`            — runs every offline self-check in one shot (CI-ready gate)

## The one thing to confirm on first live run
Adapter **selectors** (prompt box / generate button / download control) are
best-effort — confirm them against each site's logged-in UI. When a site redesigns,
fix only that one adapter. Vision-poll keeps the loop limping while a selector is stale.

## Honest risks
- CAPTCHA on frontier sites is a real arms race; the right move on a hard one is
  back off + waterfall to the next site, not brute-force (that's what gets bans).
- Free tiers are capped, not unlimited — multiple accounts + the fallback stretch it.
- Selectors drift; that's the maintenance cost of browser automation.

## v1 (archived in `archive/`)
Self-hosted Wan2.1-FLF2V + Flux on Modal. Worked end-to-end but 480p was hazy and
A100 cpu-offload was expensive. Kept for reference.
