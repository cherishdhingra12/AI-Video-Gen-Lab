# AI Video Gen Lab — v2 (autonomous browser automation)

Goal: frontier-quality video, ~free, automated. The Mac drives the free tiers of
frontier video sites through a real browser — sees, acts, learns from walls, and
resumes across reboots. Replaces v1 (self-hosted Wan on Modal: hazy + costly).

## How it works
1. `pipeline.py "idea"` — agents 1&2 (local LLM) write a script + a shot list of
   standalone per-clip prompts sharing one locked **world** (consistency anchor).
   Each clip becomes a row in `state.db`.
2. `automation/runner.py` — the loop. Picks the best site+account with credit that
   isn't cooling down, drives one clip (Playwright, human-like), polls (selectors
   first, vision if unsure), downloads the clip. A wall (captcha/rate-limit) records
   a lesson and cools that site with escalating backoff — step back, come back later.
3. When every clip of a project is done → `stitch.py` joins them (xfade) into
   `projects/<slug>/final.mp4`.

## Waterfall (quality order)
Veo → Kling → Hailuo → Vidu → **Google Vids (fallback)**. Multiple email accounts
per site multiply the daily free ceiling. Credit counts: **verify at signup** — the
engine reads them from `state.db`, nothing hardcoded.

## First-run setup
    pip install playwright && python3 -m playwright install chromium   # done
    # one-time manual login per site+account (cookies persist forever):
    python3 automation/login.py kling a1 --credits 60
    python3 automation/login.py veo   personal --credits 3
    # ... repeat per site / per email account

    python3 pipeline.py "a lantern drifting through a misty bamboo forest at night"
    python3 automation/runner.py --headed     # watch it; drop --headed for silent

## Always-on + crash-safe (as if the shutdown never happened)
    cp automation/com.aivgl.runner.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.aivgl.runner.plist
Runs the loop at wake/login + every 5 min while work remains. A shutdown mid-render
leaves a durable SQLite row the next run picks up. Proven across process death.

## Modules (each has a runnable self-check: `python3 <file>`)
- `automation/state.py`   — SQLite brain: jobs (resumable), credits, lessons (anti-ban)
- `automation/browser.py` — Playwright persistent context, stealth, human mouse/typing
- `automation/vision.py`  — screenshot → vision model (gemini-2.5-flash) → state/captcha
- `automation/learn.py`   — consult lessons, escalating cooldown, route around walls
- `automation/runner.py`  — the loop (`--selfcheck` runs it offline with a fake site)
- `automation/sites/*.py` — one tiny adapter per site (submit/poll/download)
- `stitch.py`             — ffmpeg xfade join + last-frame extract
- `agents.py`             — script + per-clip prompts with the world anchor

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
