"""
runner.py — the loop. Reads the durable brain, drives one clip at a time through
the best available site, learns from every wall, and stitches finished projects.Crash-safe by construction: every state change is committed to SQLite before the next
step, so a shutdown at any point leaves a row the next run picks up. Run it when you
need a reel (no daemon, no launchd — you start it) and it continues "as if the
shutdown never happened":

    resume mid-flight jobs first  ->  then drain the queue  ->  stitch done projects

    python3 runner.py                 # run the loop once
    python3 runner.py --once          # process a single job and exit (used by tests)

Adapters are loaded by name from sites/. The scheduler (learn.Brain.pick) chooses
site+account by remaining credit and cooldown; a Wall records a lesson and escalates
the backoff so we step back and return later instead of repeating the mistake.
"""

import importlib
import glob
import os
import re
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "sites"))
sys.path.insert(0, os.path.dirname(_DIR))       # project root, for stitch.py

from state import State                     # noqa: E402
import learn                                 # noqa: E402
from learn import Brain                      # noqa: E402
from base import Wall                        # noqa: E402  (same module identity as adapters' `from base import Wall`)

ROOT = os.path.dirname(_DIR)
CLIPS_DIR = os.path.join(ROOT, "clips")            # per-project downloaded clips
PROJECTS_DIR = os.path.join(ROOT, "projects")      # finished reels land here (final.mp4)
MAX_RETRIES = 3
REQUEUE_CAP = 2                              # times the GATE may send one clip back, then a human
DOWNLOAD_TRIES = 3                           # in-session fetch attempts before a wall (the clip exists)
POLL_EVERY = 8.0                             # seconds between poll looks
# Grouped Google family (owner ruling 2026-10-26): Vids and Flow run the same Veo generation,
# so a reel locked on one falls through to the other when the first is dry.
GOOGLE_FAMILY = ("googlevids", "flow")
POLL_TIMEOUT = 12 * 60                       # give a render up to 12 min


def _adapter(site: str):
    return importlib.import_module(f"sites.{site}").ADAPTER


def _session(site: str, account: str, headless: bool):
    from browser import Session
    return Session(site, account, headless=headless)


def _chrome_pin(site: str, account: str) -> str:
    """The Chrome profile this account's session was imported from, or '' when nothing recorded one.

    import_login.py writes this beside the session it describes. Without it the re-import re-scans
    every Chrome profile and takes the fullest — and once one site has two accounts, "fullest" is
    account 1's, so a2 would be signed straight back into a1 and into the same generation limit."""
    try:
        with open(os.path.join(ROOT, "profiles", f"{site}__{account}", ".chrome-profile")) as f:
            return f.read().strip()
    except OSError:
        return ""


def _refresh_cookies(site: str, account: str, keywords) -> None:
    """Re-hydrate a profile from real Chrome right before a run. Some sites (Google Flow)
    rotate session cookies fast, so an old snapshot import signs out within minutes; a fresh
    pull each run keeps them live. Best-effort — a still-signed-out state is caught by the
    adapter's own login Wall. READ-only on the real browser (see import_login.py)."""
    import subprocess
    cmd = [sys.executable, os.path.join(_DIR, "import_login.py"), site, account, "--browser", "chrome"]
    for kw in keywords:
        cmd += ["--keyword", kw]
    if (pin := _chrome_pin(site, account)):
        cmd += ["--profile", pin]
    try:
        r = subprocess.run(cmd, cwd=_DIR, timeout=90, capture_output=True, text=True)
    except Exception as e:
        print(f"  cookie refresh for {site}/{account} skipped: {e}")
        return
    if r.returncode:
        # Best-effort means "carry on with the cookies we already have", not "never mention it" —
        # a wrong pin would otherwise stop refreshing for good, in silence.
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        print(f"  cookie refresh for {site}/{account} failed: {tail[-1][:160] if tail else '?'}")


def read_balance(adapter, session):
    """This account's balance as read off the page we are already on, or None when the page did
    not say. None is the whole point of the function and the reason it is one function: an
    unclear read must NEVER overwrite a known number, and both callers — the runner before it
    spends, and `login.py --check` — have to hold that exact line, so they hold it here."""
    reader = getattr(adapter, "read_credits", None)
    if reader is None:
        return None
    try:
        n = reader(session)
    except Exception as e:
        print(f"  credit read skipped: {str(e)[:120]}")
        return None
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None                                  # not clear -> keep the last known number
    return n


def _observe_credits(st: State, adapter, session, site: str, account: str) -> None:
    """Best-effort: read this account's REAL balance off the page we're already on, before the
    local spend, so the scheduler and brain.report work from observed numbers instead of the
    hand-entered signup guess. Fail-open — an unclear read (None / nonsense / gateway down) keeps
    the last known number and never blocks the job."""
    n = read_balance(adapter, session)
    if n is None:
        return
    st.observe_credits(site, account, n)
    print(f"  {site}/{account}: observed {n} credits on the site")


SWEEP_MAX_AGE = 3600.0    # a balance observed within the last hour is fresh enough to choose on


def sweep_balances(st: State, headless: bool = True, max_age: float = None,
                   sites=None, log=print) -> dict:
    """Ask every site the scheduler can actually choose, what it ACTUALLY holds — before any
    reel chooses its source (owner rule, 2026-10-27).

    A ledger number is only as good as its last read: a hand-entered 50 hides an account the
    site has already emptied, and a stale 0 hides one that refilled — either way `Brain.pick`
    hands a reel to a source that cannot carry it. So the site itself is asked, and its answer
    is recorded `observed`. Google Vids has no counter anywhere (confirmed live 2026-10-22),
    so it answers None and its derived count stands — that IS the honest read for that site.

    The sweep refreshes exactly the set `pick()` ranks — `st.available()` — and nothing else:
    a pair that is cooling is not visited (stepping back means stepping back), and a dry pair
    whose refill has not arrived cannot be picked, so visiting it would only buy a page view.
    Reviving a 0/no-refill row stays a human command (`login.py --check`).

    House rules are not touched: this runs BEFORE pick and pick still decides everything —
    the Vids -> Flow waterfall, one reel one source, affordability. The sweep only makes sure
    the decision is made on the site's own numbers.

    Fail-open everywhere: a signed-out session, a dead site, a crashed read — the row survives
    and the run continues. A session the site NAMES as another account records nothing: a1's
    balance must never be written into a2's row (pool.py's disaster). Returns a summary dict
    {observed, unreadable, skipped, mismatch, failed} for the report and the self-check."""
    out = {"observed": [], "unreadable": [], "skipped": [], "mismatch": [], "failed": []}
    now = time.time()
    for c in st.available():
        site, account = c["site"], c["account"]
        if sites and site not in sites:
            continue
        if max_age is not None:
            row = st.db.execute("SELECT updated FROM credits WHERE site=? AND account=?",
                                (site, account)).fetchone()
            if row and (now - row["updated"]) < max_age:
                out["skipped"].append((site, account, "fresh"))
                continue
        try:
            adapter = _adapter(site)
            if getattr(adapter, "needs_cookie_refresh", False):
                _refresh_cookies(site, account, getattr(adapter, "cookie_keywords", []))
            with _session(site, account, headless=headless) as s:
                s.goto(adapter.home)
                time.sleep(4)      # SPA hydration: a screenshot taken too early sees the
                                   # loading shell, not the number (flow/a1, 2026-10-27)
                # whose session is this? a refresh that handed a1's cookies to a2 must not
                # write a1's balance into a2's row — ask the site before recording anything.
                who = None
                reader = getattr(adapter, "read_account", None)
                if reader is not None:
                    try:
                        who = reader(s)
                    except Exception:                    # noqa: BLE001
                        who = None
                if who and not st.account_matches(site, account, who):
                    out["mismatch"].append((site, account, who))
                    log(f"  sweep: {site}/{account} holds {who} — NOT this account; "
                        f"balance not recorded")
                    continue
                n = read_balance(adapter, s)
        except Exception as e:                           # noqa: BLE001
            out["failed"].append((site, account))
            log(f"  sweep: {site}/{account} could not be read ({str(e)[:90]})")
            continue
        if n is None:
            out["unreadable"].append((site, account))
            log(f"  sweep: {site}/{account} — the page did not state a balance; ledger untouched")
            continue
        st.observe_credits(site, account, n)
        out["observed"].append((site, account, n))
        log(f"  sweep: {site}/{account} — {n} credits observed on the site")
    return out


def _check_identity(st: State, adapter, session, site: str, account: str) -> bool:
    """Is this session the account we think it is? Ask the SITE, before a credit is spent.

    Three Google accounts share one pool of Chrome profiles, and a refresh that hands account 2
    the cookies of account 1 is a failure with no visible symptom: a2 spends a1's generation
    limit, and the per-account credit counts in the ledger — the whole point of tracking them
    separately — become fiction. The site names the account it is signed in as (Google's avatar
    carries the address), so this is one cheap check against a real fact.

    Returns True when the session may proceed. A MISMATCH is refused — but only when the site
    actually said which account it is: an empty answer means unknown, and unknown is not
    evidence of wrongdoing, so it is allowed through and said out loud."""
    reader = getattr(adapter, "read_account", None)
    if reader is None:
        return True
    try:
        email = reader(session)
    except Exception:
        return True
    if not email:
        return True                            # the page did not say — unknown is not a mismatch
    if st.account_matches(site, account, email):
        return True
    print(f"  {site}/{account}: the session is signed in as {email}, NOT the account this row "
          f"belongs to — refusing to spend its credit")
    raise Wall("login", f"wrong account: this session holds {email}, but {site}/{account} is "
                         f"{st.account_email(site, account) or 'unrecorded'}. The Chrome-profile "
                         f"pin or the cookie import is pointing at the wrong session.")


def _gave_up(job, site: str, detail: str) -> None:
    """One clip out of retries: its job is dead, so the reel it belongs to can never assemble on
    its own. The machine has nothing left here — the reel is locked to ONE site (WAVELENGTH §5),
    so there is no other site to try, and that is exactly why this is a person's."""
    import crisis
    crisis.raise_crisis(
        "clip-failed", subject=job["project"],
        detail=f"clip {job['idx']} never rendered on {site}: {detail}",
        tried=[f"{MAX_RETRIES} submit/poll rounds on {site}",
               f"no other site: the reel is locked to {site} (one reel, one source)",
               "brain cooldown escalation (hit_wall), so the next run waits it out"],
        check="clip-failure-escalation",
        needs=f"a person: render clip {job['idx']} by hand on {site} (or replace that shot)")


def _early_look(project: str, dest: str) -> list:
    """Look at the reel's FIRST clip before paying for the second one (owner fix, 2026-10-14).

    Every craft check today runs on the FINISHED reel, so a source that renders this reel frozen
    or photoreal is discovered four to six generations later — all of them already paid for. The
    first clip is the cheap tell: one ffmpeg pass and a handful of VISION calls (free-tier, not
    video credits).

    The checks are qc's OWN, run against this one clip as if it were the whole reel, so what this
    asks for and what the gate asks for can never be two different things. An UNREVIEWED (gateway
    down) is NOT a defect here — it is an outage, the gate would say the same, and a person
    decides; parking a reel on a gateway outage is the bug, not the fix."""
    import qc
    probs = []
    try:
        if qc._frozen_still(dest):
            probs.append("the clip is a frozen still (all sampled frames identical)")
    except Exception as e:                                  # noqa: BLE001 — a look that could not
        print(f"  first-look playability skipped ({str(e)[:80]})")     # run is not a defect
    try:
        reel = qc.Reel(os.path.join(PROJECTS_DIR, project))
        reel.final = dest                                   # THIS clip, judged as the reel
        probs += [p for p in qc.check_style_uniform(reel) if not isinstance(p, qc.Unreviewed)]
    except Exception as e:                                  # noqa: BLE001
        print(f"  first-look style check skipped ({str(e)[:80]})")
    return probs


def _duration(path: str) -> float:
    """A file ffprobe can time is a file that got there. 0 = missing, or not a video."""
    try:
        import qc
        return qc._duration(path)
    except Exception:                                       # noqa: BLE001
        return 0.0


def _already_downloaded(job) -> str:
    """The finished clip a row is ALREADY holding — '' when there is nothing to keep.

    Only a mid-DOWNLOAD row can be holding one: 'downloading' is the step immediately before
    'done', so a shutdown there leaves a complete file and re-driving the row would buy the same
    clip twice. A QUEUED row is never one, however tempting the file looks — the gate requeues a
    clip it rejected while the rejected file is still on disk, and keeping that file would skip
    the re-shoot the gate asked for.

    ponytail: 'ffprobe can time it' stands in for 'the fetch finished'. A truncated download that
    still carries a readable header would be kept as done — the assembly gate's own playability
    check refuses such a cut, which is where it gets caught. Upgrade to a .part file + rename if
    a truncated clip ever reaches the gate."""
    dest = os.path.join(CLIPS_DIR, job["project"], f"clip_{job['idx']:03d}.mp4")
    return dest if job["status"] == "downloading" and _duration(dest) > 0 else ""


def _park_reel(st: State, project: str, why: str) -> int:
    """Stop spending on a reel whose first clip will not come right.

    A 'failed' row is not a retry — the queue skips it and a resume does not re-drive it — so the
    reel costs nothing more until a person looks. Without this the loop simply moves on to clip 1
    and renders the other four scenes of a film that can never assemble."""
    n = 0
    for c in st.project_clips(project):
        if c["status"] == "queued":
            st.set_status(c["id"], "failed", error=f"reel parked: {why}"[:300])
            n += 1
    return n


def process_job(st: State, br: Brain, job, headless: bool = True,
                pick_override: tuple = None) -> str:
    """Drive one clip end to end. Returns final status. Any Wall -> cool down +
    requeue (not a failure). Each step commits before the next, so a crash resumes.

    `pick_override` = (site, account) skips the scheduler and drives THIS pair — used only
    by _resume_probe to test one unproven-dry account with a real queued clip."""
    # One reel, one source (the owner's rule, 2026-10-09). Once ANY clip of this project has come
    # back, that site is the reel's lock and nothing else may add a scene to it — pick(strict=True)
    # returns None while it is cooling, so the loop WAITS for the primary instead of filling in from
    # a fallback (this reel first ended up krea+vibes+googlevids, three looks in one cut).
    # BEFORE the first clip lands, the job's `site` is an opening bid: if that bid is unusable
    # (dry / disabled / hard-blocked), the WHOLE reel fails over to the next priority site
    # (whole-reel fails over, never a scene at a time — see SITE_ORDER in learn.py).
    # But a bid that is merely COOLING still gets the patience the lock gets. One `selector` wall
    # (a 120s cooldown, retry 1/3) failed a reel over from Google Vids — 12 credits left, 720x1280,
    # native audio — onto vibes: 464x832, silent, so the reel lost its narration and its
    # resolution and came back with the dialogue burned in as garbled subtitles. `want_site` is a
    # soft preference, so pick() fell straight through to vibes the moment the primary cooled.
    # Wait it out instead; fail over only when the primary is genuinely dead.
    locked = st.project_site(job["project"])
    # How many clips this reel still needs — the number that says whether a site can FINISH it.
    # Handed to pick() so the reel starts on a source that can afford the whole thing instead of
    # stranding half-made when the first site's bucket runs out (a strand costs every clip
    # already rendered: the lock means the rest waits for a refill, and one reel is one source).
    need = sum(1 for c in st.project_clips(job["project"]) if c["status"] != "done")
    if locked:
        pick = br.pick(want_site=locked, strict=True, need=need, cost_of=_adapter)
        # pick_override bypasses all scheduler logic — it's used by _resume_probe to force
        # a specific account on the locked site for the probe. Respect it before any fallback.
        if not pick and pick_override:
            pick = pick_override
        elif not pick and locked in GOOGLE_FAMILY:
            # GROUPED GOOGLE FAMILY (owner ruling, 2026-10-26): Vids and Flow run the same Veo
            # generation on Google's side, so a reel locked on one Google site falls through to
            # the OTHER Google site (a1 -> a2 -> a3 on Vids, then a1 -> a2 -> a3 on Flow) instead
            # of waiting until a dry bucket refills. Same model family, so one-look holds — this
            # is not the krea/vibes mixing the lock exists to stop.
            sibling = next((s for s in GOOGLE_FAMILY if s != locked), None)
            if sibling:
                pick = br.pick(want_site=sibling, strict=True, need=need, cost_of=_adapter)
        if not pick:
            return "waiting"
        site = pick[0]
    else:
        if br.wait_for_bid(job["site"]):
            # ponytail: waiting breaks the whole queue loop, so a parked reel stalls other
            # projects' queued jobs too. Fine while one reel is in flight at a time (how this
            # house runs); skip-to-the-next-project if a second reel is ever queued alongside.
            return "waiting"                               # a short cooldown is not a dead end
        # The choice of source is about to be made — make it on the site's own numbers
        # (owner rule, 2026-10-27): anything observed within the last hour stands, the rest
        # is re-read now. Fail-open; pick() below still decides everything.
        try:
            sweep_balances(st, headless=headless, max_age=SWEEP_MAX_AGE)
        except Exception as e:  # noqa: BLE001
            print(f"  pre-choice balance sweep skipped ({str(e)[:90]})")
        pick = br.pick(want_site=job["site"], strict=False, need=need, cost_of=_adapter)
        if not pick:
            return "waiting"                                # nothing attackable right now
        site = pick[0]
        if site != job["site"]:
            print(f"  {job['project']}: primary {job['site']} unavailable — the WHOLE reel "
                  f"moves to {site} (one reel, one source)")
            st.lock_project(job["project"], site)
    account = pick[1]
    adapter = _adapter(site)
    dest = os.path.join(CLIPS_DIR, job["project"], f"clip_{job['idx']:03d}.mp4")

    st.set_status(job["id"], "submitting", site=site, account=account)
    try:
        if getattr(adapter, "needs_cookie_refresh", False):
            _refresh_cookies(site, account, getattr(adapter, "cookie_keywords", [site]))
        with _session(site, account, headless) as s:
            adapter.submit(s, job["prompt"])
            st.set_status(job["id"], "rendering")
            _check_identity(st, adapter, s, site, account)   # the right account, before the spend
            _observe_credits(st, adapter, s, site, account)   # observed > hand-entered
            st.spend_credit(site, account, adapter.credit_cost)

            deadline = time.time() + POLL_TIMEOUT
            while True:
                # The deadline is tested AFTER the poll, so it stops the next SLEEP rather than the
                # look: one more poll lands past the timeout instead of one POLL_EVERY before it.
                # That look is worth a generation — a render that finishes in the last seconds used
                # to be walled and re-submitted from scratch, buying the same clip twice.
                state = adapter.poll(s)
                if state == "done":
                    st.set_status(job["id"], "downloading")
                    # The clip EXISTS on the site now. A failed fetch must never cost a second
                    # generation, so the download is retried while the page is still open and
                    # alive — a wall here requeues the clip, and the retry submits a brand-new
                    # render for something the site has already made.
                    for attempt in range(DOWNLOAD_TRIES):
                        try:
                            adapter.download(s, dest)
                            break
                        except Wall as w:
                            if attempt == DOWNLOAD_TRIES - 1:
                                raise
                            print(f"  download of clip {job['idx']} failed ({w.detail[:70]}) — "
                                  f"retrying in-session, the render is already on the site")
                            time.sleep(5)
                    # THE FIRST LOOK: the reel's own first clip, before the other scenes are bought
                    if job["idx"] == 0:
                        look = _early_look(job["project"], dest)
                        if look:
                            why = "; ".join(look)[:300]
                            print(f"  {job['project']}: the FIRST clip came back wrong — stopping "
                                  f"the reel here instead of rendering the rest: {why}")
                            os.replace(dest, f"{dest}.rejected-{int(time.time())}")
                            kept = _regenerate(st, os.path.join(PROJECTS_DIR, job["project"]),
                                               [job["idx"]], reason=f"the first look: {why}",
                                               check="early-look")
                            if not kept:                      # the re-shoot budget is spent
                                n = _park_reel(st, job["project"], why)
                                print(f"  {job['project']}: parked — {n} clip(s) it no longer "
                                      f"needs will not be rendered")
                                st.set_status(job["id"], "failed", error=f"first look: {why}"[:300])
                            return "reshoot"
                    st.set_status(job["id"], "done", video_path=dest)
                    br.note_success(site, account)
                    return "done"
                if state in ("captcha", "rate_limited", "login", "error"):
                    kind = {"rate_limited": "rate_limit"}.get(state, state)
                    raise Wall(kind, f"seen while polling ({state})")
                if time.time() >= deadline:
                    raise Wall("block", "render timed out")
                time.sleep(POLL_EVERY)

    except Wall as w:
        if w.kind == "credits":
            # The site said it is OUT. That is a fact about this account, not a cooldown — record
            # it in the ledger with whatever refill time the site named, so the scheduler stops
            # choosing it and the report can say when it comes back. Without this the balance sat
            # at its hand-entered guess (or at 0 with no date) and the same empty site was picked
            # again and again.
            back = learn.refill_at(w.refill)
            st.mark_dry(site, account, back)
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(back)) if back
                    else "a person says otherwise")
            print(f"  {site}/{account}: out of credits — dry until {when}")
            br.hit_wall(site, account, w.kind, step=job["status"], detail=w.detail)
            # A DRY SITE IS NOT A FLAKY CLIP, so it must not spend the clip's retries. `retries`
            # budgets the SITE's flakiness (state.requeue says so) and this attempt was refused
            # before it cost anything. Spending one here meant the third refused submit marked the
            # clip 'failed' and filed a crisis over an account that was merely empty until its
            # refill — which is exactly what the 2026-10-16 good-morning run was two walls away
            # from. The pair is dry in the ledger, so no run picks it again before `back`.
            st.set_status(job["id"], "queued", error=f"credits: {w.detail} — dry until {when}"[:300])
            return "dry"
        if w.kind == "login":
            # A SIGNED-OUT SESSION IS A FACT ABOUT THE ACCOUNT, not about this clip. The same
            # reasoning as the credits wall above: the attempt was refused before it cost
            # anything, so it must not spend the clip's retry budget. It did (2026-10-22): three
            # Google sessions expired at once, and each wall took one of clip 3's three retries —
            # so two more would have marked the clip 'failed' and filed a crisis over a session
            # that a person re-signs in under a minute. The retry is given BACK, the pair is
            # cooled, and the reel waits for the sign-in.
            cd = br.hit_wall(site, account, w.kind, step=job["status"], detail=w.detail)
            st.set_status(job["id"], "queued",
                          error=f"login: {w.detail} — the session is signed out; "
                                f"a person must sign it back in (cool {cd:.0f}s)"[:300])
            print(f"  wall on {site}/{account}: login -> cool {cd:.0f}s — the clip's retry is NOT "
                  f"spent (a signed-out session is the account's, not the clip's)")
            return "walled"
        cd = br.hit_wall(site, account, w.kind, step=job["status"], detail=w.detail)
        n = st.bump_retry(job["id"])
        # step back: requeue unless we've exhausted retries on this clip
        st.set_status(job["id"], "queued" if n < MAX_RETRIES else "failed",
                      error=f"{w.kind}: {w.detail} (cooldown {cd:.0f}s)")
        print(f"  wall on {site}/{account}: {w.kind} -> cool {cd:.0f}s, retry {n}/{MAX_RETRIES}")
        if n >= MAX_RETRIES:
            _gave_up(job, site, f"{w.kind}: {w.detail}")
        return "walled"
    except Exception as e:                          # unexpected -> record, requeue
        n = st.bump_retry(job["id"])
        st.set_status(job["id"], "queued" if n < MAX_RETRIES else "failed", error=str(e)[:300])
        print(f"  error on {site}/{account}: {e}")
        if n >= MAX_RETRIES:
            _gave_up(job, site, str(e)[:200])
        return "error"


def _archive_cut(project_dir):
    """Move a cut the gate just rejected out of the way. `stitch_done_projects` skips any project
    that already has a final*.mp4 — so re-shooting a clip WITHOUT moving the rejected cut leaves
    the new clip on disk and the old reel in place, and nothing ever rebuilds. Moving it is also
    honest: the signature a human put on that cut (qc eye_review, keyed to qc.Reel.fp) is a
    signature on a picture that no longer exists."""
    moved = []
    if not os.path.isdir(project_dir):
        return moved                    # nothing on disk to move (the first look can fire first)
    for f in os.listdir(project_dir):
        if f in ("final_veo.mp4", "final.mp4"):
            d = os.path.join(project_dir, "rejected")
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, f"{int(time.time())}-{f}")
            os.replace(os.path.join(project_dir, f), dst)
            moved.append(os.path.basename(dst))
    return moved


def _regenerate(st: State, project_dir: str, bad, reason: str = None, check: str = "") -> list:
    """Turn a gate finding into a RE-SHOOT: requeue exactly the clips that broke the look, with
    the reason folded into their prompts, and stop after REQUEUE_CAP attempts.

    The cap is the whole point. `state.requeue` clears `retries` on purpose — that budget belongs
    to a flaky SITE. So without a counter of its own, "the gate rejected clip 2" → "re-shoot clip
    2" → "the gate rejected clip 2" never ends; it just burns generations. Twice proves the
    feedback is not the problem. After that the clip is a defect only a person can settle, and it
    is reported as one instead of re-rolled a third time.

    `bad` is a list of clip file paths (or idx ints). Returns the requeued idxs."""
    project = os.path.basename(project_dir.rstrip("/"))
    rows = {c["idx"]: c for c in st.project_clips(project)}
    by_path = {os.path.abspath(c["video_path"]): c["idx"] for c in rows.values()
               if c["video_path"]}
    idxs, spent, unknown = [], [], []
    for b in bad:
        i = b if isinstance(b, int) else by_path.get(os.path.abspath(b))
        if i is None or i not in rows:
            unknown.append(os.path.basename(str(b)))
        elif rows[i]["requeues"] >= REQUEUE_CAP:
            spent.append(i)
        elif i not in idxs:
            idxs.append(i)
    if unknown:
        print(f"  {project}: cannot re-shoot {', '.join(unknown)} — no job row for it "
              "(clip swept, or not generated by this runner)")
    if spent:
        # The machine's LAST option, spent. A gate that keeps catching the same defect after every
        # re-shoot is not asking for another render — it is asking for a check that catches this
        # BEFORE the render, which is a person's job to write. Record it as one.
        import crisis
        crisis.raise_crisis(
            "requeue-capped", subject=project,
            detail=f"clip(s) {spent} rejected by the gate again after {REQUEUE_CAP} re-shoots: "
                   f"{reason or 'no reason recorded'}",
            tried=[f"{REQUEUE_CAP} re-shoots, the rejection folded into the prompt each time",
                   "the gate that caught it runs on the ASSEMBLED cut — after the generation",
                   "no other site: the reel is locked to one source"],
            check=f"preflight-{check or 'style-uniform'}",
            needs=f"a person: settle clip(s) {spent} (re-shoot by hand, or cut the shot)",
            st=st)
    if not idxs:
        return []
    print(f"  {project}: re-shooting clip(s) {idxs} — {reason or 'rejected by the gate'}")
    for i in idxs:
        base = rows[i]["prompt"] or ""
        # append once, never per attempt: a prompt that grows a note per re-shoot is its own bug
        if "RE-SHOOT —" not in base:
            base += f"\n\nRE-SHOOT — the previous render of this clip was rejected: {reason}"
        st.requeue(project, [i], prompt=base)
    _archive_cut(project_dir)
    return idxs


def stitch_done_projects(st: State):
    """Any project whose every clip is done -> ASSEMBLE the finished reel (once).

    Built by the ONE assembler, not the old plain stitch: cards placed over the frames, the
    score bed, the dissolves, dead air trimmed, and the result verified to actually play. The
    plain stitch produced a silent, uncaptioned reel — which is not the artifact the owner
    signs off, so the engine could never finish a reel on its own.

    A project that already has a final_veo.mp4 is left alone, so a cut that already exists
    (the owner's own, or a previous run of this pass) is never overwritten. Each project is
    independent: one stale row must not abort the pass and strand every other reel."""
    import assemble
    seen = {r["project"] for r in st.db.execute("SELECT DISTINCT project FROM jobs")}
    for project in seen:
        clips = st.project_clips(project)
        if not clips or not all(c["status"] == "done" for c in clips):
            continue
        pdir = os.path.join(PROJECTS_DIR, project)
        final = os.path.join(pdir, "final_veo.mp4")
        # BOTH names, not just the engine's. qc.Reel.FINALS is this project's own definition of a
        # finished reel, and the craft-path reels shipped as final.mp4 — checking only final_veo.mp4
        # meant every pass re-attempted the four legacy reels, ran the vision style pre-flight over
        # ~18 of their clips and rejected them for the medium their own identity never declared.
        import qc
        finals_here = [os.path.join(pdir, f) for f in qc.Reel.FINALS
                       if os.path.exists(os.path.join(pdir, f))]
        if finals_here:
            cut = max(finals_here, key=os.path.getmtime)
            # A finished cut is left alone — EXCEPT one that predates its own captions.
            # diwali-purana-sandook (2026-10-23): the cut was stitched with no captions.json on
            # disk (the caption pass was stalled on a gateway outage), this guard then made the
            # absence permanent, and the caption pass only ever wrote the artifacts while
            # nothing could re-stitch. So the guard listens for exactly that one stale-cut
            # condition: a caption SPEC file newer than the cut means the cut predates its own
            # words — archive it and re-stitch below. Specs only (captions*.json), never the
            # card PNGs: build re-renders those itself, so counting them re-stitches every pass.
            specs = sorted(os.path.join(pdir, f) for f in os.listdir(pdir)
                           if f.startswith("captions") and f.endswith(".json")) \
                if os.path.isdir(pdir) else []
            newer = [f for f in specs if os.path.getmtime(f) > os.path.getmtime(cut)]
            if not newer:
                continue                              # already finished — never overwrite a cut
            _archive_cut(pdir)
            print(f"  {project}: cut predates its captions "
                  f"({', '.join(os.path.basename(f) for f in newer)}) — archiving and re-stitching")
        paths = [c["video_path"] for c in clips]
        missing = [p for p in paths if not p or not os.path.exists(p)]
        if missing:
            print(f"  {project}: {len(missing)}/{len(paths)} clip file(s) missing "
                  f"(clips cleaned up? {os.path.basename(missing[0])}) — skipping assemble")
            continue
        os.makedirs(pdir, exist_ok=True)
        try:
            # pass CLIPS_DIR explicitly: assemble would otherwise resolve clips/ from the repo
            # root on its own, ignoring the dir this loop (and its self-check) was pointed at
            assemble.build(pdir, clips_dir=os.path.join(CLIPS_DIR, project), absorb=False)
        except assemble.StyleDrift as e:
            # NOT "will retry next run" — the same clips rebuild the same cut, which is what made
            # this a livelock. Name the clips, send them back, and let the cap end it.
            if os.path.exists(final):
                os.unlink(final)
            _regenerate(st, pdir, [p for p, _ in e.bad], e.reason, check="style-uniform")
            continue
        except Exception as e:                        # this project only — keep the pass going
            print(f"  {project}: assemble failed ({str(e)[:160]}) — will retry next run")
            # any final_veo.mp4 here was written by THIS failed attempt (the guard above
            # returned early if one already existed) — drop it rather than let a partial
            # render pass as a finished cut on the next run
            if os.path.exists(final):
                os.unlink(final)
            continue
        print(f"ASSEMBLED {project} -> {final} ({len(paths)} clips)")
        _gate(st, pdir)


def _absorb(project_dir: str, st: State = None):
    """Feed the finished artifact back into the brain. This is the CONTINUOUS self-learning
    step the owner asked for: after every generation the brain audits what it actually shipped
    (captions / VO / moral / score) and records it, filing a candidate for any layer it is
    supposed to ship and didn't. Fail-open by contract — bookkeeping must never fail a build.
    """
    try:
        import brain
        # BORROW the loop's connection: pass `st` and DON'T close it — the runner still has
        # queued jobs to drive after this. Closing a shared State here killed the rest of the
        # loop ("Cannot operate on a closed database"), so ownership stays with the caller.
        b = brain.LocalBrain(st)
        r = b.absorb(project_dir)
        if r.get("ok") and r.get("learned"):
            print(f"  BRAIN absorbed {r['reel']}: filed {len(r['learned'])} layer gap(s) — "
                  + ", ".join(r["learned"]))
        elif r.get("ok"):
            print(f"  BRAIN absorbed {r['reel']}: all craft layers present")
    except Exception as e:
        print(f"  brain absorb skipped ({str(e)[:120]})")


def _gate(st: State, project_dir: str):
    """Run the craft gate over a finished reel and log the verdict. Advisory in the
    autonomous loop (never crashes the drain); block/gate items mean don't hand off / go_live.

    The gate runs BEFORE _absorb, and that order is load-bearing: absorb records the generation
    fact — including the QC result — so absorbing first memorised the PREVIOUS run's verdict and
    every reel's brain record lagged one generation behind reality."""
    try:
        import qc
        v = qc.run(project_dir, st)
        for b in v["block"]:
            print(f"  QC BLOCK: {b}")
        for g in v["gate"]:
            print(f"  QC PUBLISH-GATE: {g}")
        if v.get("eye"):
            print(f"  QC BY-EYE: {len(v['eye'])} check(s) could not run — the sheet and frames are "
                  f"in {project_dir} (eye_review.json); sign off with "
                  f"qc.py {project_dir} --signed-by '<name>'")
        # `ok` is the automated gate; `live` is the one that ships — a reel whose only gate items
        # were checks that could NOT run goes live once a human signs off the cut (WAVELENGTH §6).
        _state = ("PASS" if v["ok"] else "LIVE (signed off)" if v.get("live") else "HELD")
        print(f"  QC {project_dir}: {_state} "
              f"({len(v['block'])} block, {len(v['gate'])} gate, {len(v['checklist'])} to eyeball) "
              "— verdict saved to state.db")
        _file_findings(st, v)
        # A finding the machine can ACT on does not wait for a human. The gate named the clips;
        # send them back. After REQUEUE_CAP attempts _regenerate stops and says so, so the loop
        # cannot run forever and the owner still gets told when a person is genuinely needed.
        if v.get("reject"):
            _regenerate(st, project_dir, [i for r in v["reject"] for i in r["clips"]],
                        "; ".join(r["reason"] for r in v["reject"]),
                        check="; ".join(r["check"] for r in v["reject"] if r.get("check")))
    except Exception as e:
        print(f"  QC skipped ({e})")
    _absorb(project_dir, st)


def _file_findings(st: State, v: dict):
    """A defect the gate found must leave something behind, or the next reel makes it again.

    It files a CANDIDATE, not a rule. The brain proposes and the owner vets (state.propose_lesson
    → candidates → promote), because the alternative — a machine writing its own brief from one
    bad render — is how a project talks itself into a rule nobody chose. What the candidate buys
    is the count: keyed by the CHECK, so the same defect across ten reels lands as ONE row with
    hits=10 instead of ten near-duplicates, and a defect that RECURS is visible instead of
    anecdotal. Until promoted it changes nothing by itself.

    Fail-open by contract: bookkeeping must never fail a build."""
    try:
        for slug, why in _findings(v).items():
            st.propose_lesson(
                slug=f"defect-{slug}",
                title=f"The {slug} check keeps catching this on generated reels",
                why=f"Found by the craft gate. A recurrence count here means the generation side "
                    f"is still doing it, not that the reel was unlucky.",
                evidence=f"{v['reel']}: {why}")
    except Exception as e:  # noqa: BLE001
        print(f"  (findings not filed: {str(e)[:90]})")


def _findings(v: dict) -> dict:
    """Every defect in a verdict, keyed by the check that caught it: {check_slug: one example}.
    The check slug is read from the `[slug]` prefix qc.run writes, so nothing here needs to know
    the list of checks."""
    out = {}
    for line in v.get("block", []) + v.get("gate", []) + v.get("warn", []):
        m = re.match(r"\[([a-z0-9-]+)\]\s*(.*)", line or "")
        if m:
            out.setdefault(m.group(1), m.group(2))
    for r in v.get("reject", []):
        out.setdefault(r["check"], f"clip {', '.join(str(i) for i in r['clips'])}: {r['reason']}")
    return out


def _refused_recently(st: State, site: str, account: str, hours: float = 24.0) -> bool:
    """Did the SITE itself answer about this account recently? Then its answer is current
    knowledge — do not re-ask. The probe exists for accounts the ledger has NOT heard from
    (2026-10-24: googlevids/a1 sat at 0 with no refill date since Oct 21–22's refusals while
    the SAME site told a3 that month's reset is monthly, Nov 1), not for an account that was
    refused an hour ago."""
    try:
        rows = st.recent_lessons(site, account, kind="credits", limit=1)
    except Exception:  # noqa: BLE001 — a ledger that will not answer must not cost a probe
        return False
    return bool(rows) and (time.time() - rows[0]["created"]) < hours * 3600.0


def _resume_probe(st: State, br: Brain, job, locked: str, headless: bool = True):
    """ONE probe per run, exactly when a locked reel's own source can produce nothing.

    `available()` can never offer an account that is at 0 with NO refill date again — for a
    refusal that named no period, that date never arrives, so the rotor would skip it
    forever while the site may have refilled (and the reel locked on this source would wait
    with it: one reel, one source). This walks the unproven-dry accounts oldest-first and
    drives the NEXT QUEUED CLIP at the first one: the probe IS the production try. A
    refilled account just renders the reel's real clip; an empty one is refused at submit
    (no generation spent, no retry burned — the credits-wall handler keeps `retries`) and
    the ledger learns the date from the site's own words. Either way the answer is real,
    verified by the site, replacing a ledger entry nobody confirmed.

    Refusals of today are not stale knowledge — _refused_recently keeps those out. Returns
    the probe's process_job result, or None (no probe: not locked, a short cooldown, a
    funded-but-busy source, nothing unproven)."""
    if not locked or br.pick(want_site=locked, strict=True, proven_only=True) \
            or br.wait_for_bid(locked):
        return None
    cands = [a for a in st.unproven_dry(locked) if not _refused_recently(st, locked, a)]
    if not cands:
        return None
    account = cands[0]
    print(f"  {job['project']}: '{locked}' can produce nothing right now, but "
          f"{locked}/{account} is marked dry with NO refill date — a ledger entry nobody "
          f"has verified. Probing it with the REAL next clip: a refilled account just "
          f"renders it; an empty one is refused at submit for nothing."
          f"  ({st.credit_line(locked, account)})")
    return process_job(st, br, job, headless=headless, pick_override=(locked, account))


def loop(once: bool = False, headless: bool = True):
    st, br = State(), Brain()
    # 0) prime the brain: re-seed its canonical lessons so one added to qc.LESSONS since the last
    # run reaches THIS one (the pre-generation trigger, at the driving end). Fail-open.
    try:
        import qc as _qc
        _qc.seed(st)
    except Exception as e:  # noqa: BLE001
        print(f"  brain preflight skipped ({str(e)[:100]})")
    # 0.5) the ledger's numbers are only as good as their last read: before any reel chooses
    # its source, ask every choosable site what it ACTUALLY holds (owner rule, 2026-10-27).
    # Fail-open — a sweep that cannot run must never stop the run.
    try:
        sweep_balances(st, headless=headless)
    except Exception as e:  # noqa: BLE001
        print(f"  balance sweep skipped ({str(e)[:100]})")
    # 1) resume anything caught mid-flight by a shutdown — as if it never happened
    for job in st.resume():
        # NEVER RE-SUBMIT A RENDER THAT ALREADY EXISTS: a shutdown between the download and the
        # 'done' commit leaves the finished clip on disk, and re-driving that row buys it again.
        kept = _already_downloaded(job)
        if kept:
            st.set_status(job["id"], "done", video_path=kept)
            print(f"  clip {job['idx']} of {job['project']} was already downloaded — keeping it, "
                  f"not rendering it again")
            continue
        print(f"resuming clip {job['project']}#{job['idx']} (was {job['status']})")
        st.set_status(job["id"], "queued")          # re-drive from a clean step
    # 2) drain the queue
    while True:
        job = st.next_queued()
        if not job:
            break
        print(f"clip {job['project']}#{job['idx']}: {job['prompt'][:50]}...")
        r = process_job(st, br, job, headless=headless)
        if r == "waiting":                           # the reel's own source is cooling/dry
            print("the reel's source is cooling down or out of credit — waiting on it rather "
                  "than adding a scene from another site; will resume next run.")
            _pr = _resume_probe(st, br, job, st.project_site(job["project"]), headless=headless)
            if _pr:
                print(f"  probe finished ({_pr})")
            break
        if once:
            break
    stitch_done_projects(st)
    # Whatever the machine could not finish on its own, said out loud at the end of the run — a
    # crisis is a person's turn, and the last line of the run is where a person looks. Fail-open:
    # a broken to-do list must never be the thing that kills the loop.
    try:
        import crisis
        open_ = crisis.open_crises()
        if open_:
            print(f"\n{len(open_)} CRISIS(es) open — a person's turn "
                  f"(python3 automation/crisis.py --list):")
            for p, r in open_:
                print(f"  [{r['kind']}] {r['subject']}: {r['needs'] or 'a person'}")
    except Exception as e:  # noqa: BLE001
        print(f"  (crisis list not read: {str(e)[:90]})")
    st.close()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        # offline self-check: a FAKE adapter proves the loop's control flow without
        # a browser/network — resume, wall->requeue->cooldown, success->done->stitch.
        import tempfile, types
        import state as state_mod
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        st = State(db)
        br = Brain(st)
        st.set_credits("kling", "a1", remaining=100)

        # The first look judges clip 0 with qc's OWN medium check, which is a live vision call —
        # and this self-check promises no network. Stub it once for the whole block: the
        # classifier is qc's dependency and qc's own self-check is where it is tested.
        import vision
        vision.medium_of = lambda png: "3d"

        # a fake adapter: first call walls (captcha), second succeeds and writes a clip
        calls = {"n": 0}
        fake = types.SimpleNamespace(name="kling", credit_cost=10)
        fake.read_credits = lambda s: 42          # the site shows 42 credits for this account
        def fake_submit(s, prompt): calls["n"] += 1
        def fake_poll(s):
            return "captcha" if calls["n"] == 1 else "done"
        def fake_download(s, dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            import subprocess
            # a real clip CARRIES ITS OWN AUDIO — that is where the narration lives
            # (WAVELENGTH.md §3), and a silent clip is a defect the assembler must reject — and it
            # MOVES: a solid colour is a frozen still, which the first look now catches.
            subprocess.run(["ffmpeg","-y","-f","lavfi","-i","testsrc=size=720x1280:rate=24:duration=6",
                            "-f","lavfi","-i","sine=frequency=440:d=6",
                            "-pix_fmt","yuv420p","-shortest",dest],
                           capture_output=True, check=True)
            return dest
        fake.submit, fake.poll, fake.download = fake_submit, fake_poll, fake_download

        # monkeypatch loader + session (context manager that yields None)
        import runner
        runner._adapter = lambda site: fake
        class _NullSession:
            def __enter__(self): return None
            def __exit__(self, *a): return False
        runner._session = lambda site, account, headless: _NullSession()
        runner.POLL_EVERY = 0.01
        # hermetic: point the loop's output dirs at a temp dir so this self-check never
        # writes clips or reels into the real workspace.
        _tmp = tempfile.mkdtemp()
        runner.CLIPS_DIR = os.path.join(_tmp, "clips")
        runner.PROJECTS_DIR = os.path.join(_tmp, "projects")
        import crisis
        crisis.CRISES = os.path.join(_tmp, "crises")   # a self-check must not file a real crisis

        jid = st.add_job("selftest", 0, "a blue clip")
        # 1st pass: hits the captcha wall -> requeued + cooled
        assert runner.process_job(st, br, st.next_queued(), headless=True) == "walled"
        assert st.cooldown_left("kling", "a1") > 0, "wall must cool the site"
        # clear cooldown to simulate 'came back later', 2nd pass: succeeds
        st.db.execute("UPDATE lessons SET cooldown_until=0"); st.db.commit()
        assert runner.process_job(st, br, st.next_queued(), headless=True) == "done"
        clip = st.project_clips("selftest")[0]
        assert clip["status"] == "done" and os.path.exists(clip["video_path"])
        # the observed balance drove the local count: 42 read off the page - 10 spent = 32
        row = st.db.execute("SELECT remaining, source FROM credits WHERE site='kling'").fetchone()
        assert row["remaining"] == 32 and row["source"] == "observed", dict(row)

        # OUT OF CREDITS IS A FACT, NOT A COOLDOWN. A site that says it is empty must be marked dry
        # in the ledger with the refill time it named — otherwise the balance keeps its hand-entered
        # guess, the scheduler picks the same empty site again, and the report calls it 'manual'
        # forever. The wall kind carries the period; the runner turns it into a time.
        cst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        cbr = Brain(cst)
        cst.set_credits("flow", "f1", remaining=5)          # funded ON PAPER
        cst.set_credits("krea", "k1", remaining=40)         # where the reel must move to
        cwall = types.SimpleNamespace(name="flow", credit_cost=1)
        def cwall_submit(s, prompt):
            raise Wall("credits", "Flow daily credits spent — refreshes next day", refill="daily")
        cwall.submit, cwall.poll = cwall_submit, lambda s: "done"
        cwall.download = lambda s, dest: dest
        runner._adapter = lambda site: cwall if site == "flow" else fake
        cst.add_job("dryreel", 0, "a clip on a site with nothing left")
        _dj = cst.db.execute("SELECT * FROM jobs WHERE project='dryreel' AND idx=0").fetchone()
        assert runner.process_job(cst, cbr, _dj, headless=True) == "dry"
        _dj = cst.db.execute("SELECT * FROM jobs WHERE id=?", (_dj["id"],)).fetchone()
        assert _dj["retries"] == 0 and _dj["status"] == "queued", \
            f"a dry site must not spend the clip's retries — three refusals kill a clip: {dict(_dj)}"
        assert "Flow daily credits spent" in _dj["last_error"], \
            f"the site's own words must reach the report: {_dj['last_error']}"
        row = cst.db.execute(
            "SELECT remaining, resets_at, source FROM credits WHERE site='flow'").fetchone()
        assert row["remaining"] == 0 and row["source"] == "observed", dict(row)
        assert row["resets_at"] > time.time(), "the refill time the site named is in the ledger now"
        assert ("flow", "f1") not in {(c["site"], c["account"]) for c in cst.available()}, \
            "an empty site must not be offered again"
        assert cbr.wait_for_bid("flow") is False, "an empty site is a dead end, not a wait"
        assert cbr.pick(want_site="flow") == ("krea", "k1"), \
            "the reel moves to a source that has credit"
        # ...and a SECOND ACCOUNT on that same site un-blocks it WITHOUT moving the reel — which is
        # the entire reason to keep a second account (the free ceiling is per account, not per site).
        # It only works while the lock records the SITE: record the account and clips 1-4 follow a1's
        # empty bucket onto another site, which is a different look and a strand.
        assert cbr.pick(want_site="flow", strict=True) is None, \
            "the reel's locked site is empty and alone — so the reel waits"
        cst.set_credits("flow", "f2", remaining=50)
        assert cbr.pick(want_site="flow", strict=True) == ("flow", "f2"), \
            "a funded second account must be picked on the reel's own site"
        print("OK: a site that says it is out of credits is marked dry with its own refill time")
        print("OK: a second account on the same site un-blocks the reel without changing its look")

        # A SIGNED-OUT SESSION IS THE ACCOUNT'S FACT, NOT THE CLIP'S. Three Google sessions
        # expired at once on 2026-10-22 and each wall took one of clip 3's three retries — so
        # two more would have marked the clip 'failed' and filed a crisis over a session a
        # person re-signs in under a minute. The login wall must leave the retry budget alone.
        lst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        lbr = Brain(lst)
        lst.set_credits("googlevids", "g1", remaining=20)
        lwall = types.SimpleNamespace(name="googlevids", credit_cost=1)
        def lwall_submit(s, prompt):
            raise Wall("login", "Choose an account")
        lwall.submit, lwall.poll = lwall_submit, lambda s: "done"
        lwall.download = lambda s, dest: dest
        runner._adapter = lambda site: lwall if site == "googlevids" else fake
        lst.add_job("loginreel", 0, "a clip on a site whose session died")
        _lj = lst.db.execute("SELECT * FROM jobs WHERE project='loginreel' AND idx=0").fetchone()
        assert runner.process_job(lst, lbr, _lj, headless=True) == "walled"
        _lj = lst.db.execute("SELECT * FROM jobs WHERE id=?", (_lj["id"],)).fetchone()
        assert _lj["retries"] == 0 and _lj["status"] == "queued", \
            f"a signed-out session must not spend the clip's retries: {dict(_lj)}"
        assert "signed out" in _lj["last_error"], \
            f"the report must say what a person has to do: {_lj['last_error']}"
        print("OK: a signed-out session does not spend the clip's retries")

        # ONE REEL, ONE SOURCE. "selftest" now has a done clip on kling, so kling is the reel's
        # lock. Put kling in cooldown and stand up a healthy other site: the next clip must WAIT
        # for kling rather than let pika add a differently-shot scene. This is the bug that
        # shipped a krea+vibes+googlevids reel — job["site"] alone let a wall reroute the reel.
        st.set_credits("pika", "a1", remaining=50)
        br.hit_wall("kling", "a1", "block")            # cool the reel's own source
        st.add_job("selftest", 9, "a clip that must wait")
        _wait = st.db.execute("SELECT id FROM jobs WHERE project='selftest' AND idx=9").fetchone()
        _jr = st.db.execute("SELECT * FROM jobs WHERE id=?", (_wait["id"],)).fetchone()
        assert runner.process_job(st, br, _jr, headless=True) == "waiting", \
            "a locked reel must wait for its own source, not fill in from another site"
        st.db.execute("UPDATE lessons SET cooldown_until=0"); st.db.commit()
        assert runner.process_job(st, br, _jr, headless=True) != "waiting", \
            "the wait must end the moment the reel's own source is available again"
        print("OK: one reel = one source — a locked reel WAITS for its own site, never mixes")

        # THE RESUME PROBE (owner ask, 2026-10-24): the three Google accounts sit in one
        # rotation, and an account refused with NO period in its words lands in the ledger as
        # 0 with no refill date — which `available()` can never pick again, so the rotor
        # skips it FOREVER while the site may have refilled. At the moment a locked reel is
        # about to stop on that very source, the loop PROBES the unproven account with the
        # real queued clip instead of waiting in the dark: a refilled account just renders
        # it; an empty one is refused at submit for nothing, and the ledger learns the date
        # from the site's own words. The lock here is EARNED — clip 0 really rendered on
        # googlevids, so project_site() owns the reel and the probe can never open it to
        # another site.
        _saved_adapter = runner._adapter
        pkt = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        pkb = Brain(pkt)
        pkt.set_credits("googlevids", "p1", remaining=2)      # funded at the reel's start
        pkt.set_credits("flow", "f1", remaining=50)           # the healthy tier: must NOT be touched

        def _probe_adapter(empty_line):
            def submit(s, prompt):
                if empty_line:
                    raise Wall("credits", empty_line, refill="monthly")
            def download(s, dest):                # a real file — the first look must pass it
                import subprocess as _sp
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                _sp.run(["ffmpeg", "-y", "-f", "lavfi",
                         "-i", "testsrc=size=720x1280:rate=24:duration=4",
                         "-f", "lavfi", "-i", "sine=frequency=440:d=4",
                         "-pix_fmt", "yuv420p", "-shortest", dest],
                        capture_output=True, check=True)
                return dest
            return types.SimpleNamespace(name="googlevids", credit_cost=1, submit=submit,
                                         poll=lambda s: "done",
                                         download=download,
                                         read_credits=lambda s: None)

        good = _probe_adapter(None)                    # the account works while it has credit
        dry = _probe_adapter("You've reached your monthly limit. Wait until Nov 1 at 12:30 "
                             "PM for limits to reset or upgrade now.")
        pkt.add_job("probe-proj", 0, "a scene that renders", site="googlevids")
        _p0 = pkt.db.execute("SELECT * FROM jobs WHERE project='probe-proj'").fetchone()
        runner._adapter = lambda site: good
        assert runner.process_job(pkt, pkb, _p0, headless=True) == "done"
        assert pkt.project_site("probe-proj") == "googlevids", \
            "the test must earn the lock: project_site() comes from a DONE clip"
        pkt.add_job("probe-proj", 1, "the reel's own next scene", site="googlevids")
        _p1 = pkt.db.execute("SELECT * FROM jobs WHERE project='probe-proj' AND idx=1").fetchone()
        # THE OWNER'S CHAIN (2026-10-27): an unproven 0 — the site has never answered — is a
        # guess, and a guess may not gate the chain. The reel ATTEMPTS the site, and the
        # refusal (free: no generation happens when the site is out) moves the chain on:
        # googlevids/p1 refuses with its monthly words, the family falls to flow/f1, which
        # refuses too; only then does the reel WAIT.
        pkt.db.execute("UPDATE credits SET remaining=0, resets_at=0 "
                       "WHERE site='googlevids' AND account='p1'")
        pkt.db.execute("UPDATE credits SET remaining=0, resets_at=0 "
                       "WHERE site='flow' AND account='f1'")
        pkt.db.commit()

        runner._adapter = lambda site: dry
        _r = runner.process_job(pkt, pkb, _p1, headless=True)
        assert _r == "dry", f"the chain attempts an unproven 0 — the site refuses for free: {_r}"
        assert pkt.db.execute("SELECT resets_at FROM credits "
                              "WHERE site='googlevids' AND account='p1'").fetchone()["resets_at"] > time.time(), \
            "the refusal's own words reached the ledger as the refill fact"
        _r = runner.process_job(pkt, pkb, _p1, headless=True)
        assert _r == "dry", f"the family falls to flow/f1, which refuses too: {_r}"
        assert runner.process_job(pkt, pkb, _p1, headless=True) == "waiting", \
            "every Google pair now carries the site's own refusal — the reel waits"
        # back to the unproven state for the probe tests below
        pkt.db.execute("UPDATE credits SET remaining=0, resets_at=0 "
                       "WHERE site IN ('googlevids','flow')")
        pkt.db.execute("DELETE FROM lessons"); pkt.db.commit()
        # (a) a site that refused THIS account within the last day already answered — do not re-ask
        pkb.hit_wall("googlevids", "p1", "credits", detail="refused within the hour")
        assert runner._resume_probe(pkt, pkb, _p1, "googlevids", headless=True) is None, \
            "a fresh site refusal is current knowledge, not stale — wait, do not re-ask"
        # (b) an unproven one (no site answer in 24h) IS probed, once, with the real clip
        pkt.db.execute("DELETE FROM lessons"); pkt.db.commit()
        assert runner._resume_probe(pkt, pkb, _p1, "googlevids", headless=True) == "dry"
        assert pkt.project_clips("probe-proj")[0]["site"] == "googlevids", \
            "the probe renders the reel's OWN source — it never opens the reel to another site"
        assert pkt.db.execute(
            "SELECT MAX(remaining) FROM credits WHERE site='flow'").fetchone()[0] == 0, \
            "the probe must not touch a site the reel is not locked on"
        _row = pkt.db.execute("SELECT remaining, resets_at, source FROM credits "
                             "WHERE site='googlevids' AND account='p1'").fetchone()
        assert _row["remaining"] == 0 and _row["source"] == "observed", dict(_row)
        assert _row["resets_at"] > time.time() + 3 * 24 * 3600, \
            (f"the site's OWN monthly words must reach the ledger as a real future date, "
             f"got {dict(_row)} — the daily default woke a day early twice on 2026-10-24")
        # (c) once refused, the account is PROVEN dry — never probed again this period
        assert pkt.unproven_dry("googlevids") == [], dict(_row)
        assert runner._resume_probe(pkt, pkb, _p1, "googlevids", headless=True) is None, \
            "a proven-dry account must be waited on, not re-probed"
        # (d) a reel with no lock (or a merely-cooling bid) never triggers a probe
        assert runner._resume_probe(pkt, pkb, _p1, None, headless=True) is None
        runner._adapter = _saved_adapter
        print("OK: the resume probe asks ONE dry-without-date account per run, with the "
              "real clip it would render — verified facts replace unverified dryness")

        # WHOLE-REEL FAILOVER (owner, 2026-10-10): before ANY clip lands, a primary the SITE
        # refuses moves the ENTIRE reel to the next waterfall site, and every queued scene
        # follows. (The chain attempts an unproven ledger 0 first — the refusal, free of any
        # generation, is what proves the dry — owner rule 2026-10-27.) Once a clip is done,
        # project_site() owns the lock and nothing may move the reel again.
        fst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        fbr = Brain(fst)
        fst.set_credits("googlevids", "g1", remaining=0)    # primary at an unproven 0
        fst.set_credits("flow", "f1", remaining=8)          # next in waterfall
        runner._adapter = lambda site: fake
        runner._session = lambda site, account, headless: _NullSession()
        fake2 = types.SimpleNamespace(name="flow", credit_cost=2)
        fake2.submit = lambda s, prompt: None
        fake2.poll = lambda s: "done"
        def fake2_dl(s, dest):
            import subprocess as _sp
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _sp.run(["ffmpeg","-y","-f","lavfi","-i","testsrc=size=720x1280:rate=24:duration=4",
                     "-f","lavfi","-i","sine=frequency=440:d=4",
                     "-pix_fmt","yuv420p","-shortest",dest],
                    capture_output=True, check=True)
        fake2.download = fake2_dl
        fake2.read_credits = lambda s: None
        fjid = fst.add_job("failover-proj", 0, "a scene", site="googlevids")
        fst.add_job("failover-proj", 1, "another scene", site="googlevids")
        runner._adapter = lambda site: dry if site == "googlevids" else fake2
        _r = runner.process_job(fst, fbr, fst.next_queued("failover-proj"), headless=True)
        assert _r == "dry", f"the chain attempts the unproven primary 0 — the site refuses: {_r}"
        assert runner.process_job(fst, fbr, fst.next_queued("failover-proj"), headless=True) == "done", \
            "a refused primary fails the WHOLE reel over to flow on the next pass"
        _all = fst.project_clips("failover-proj")
        assert all(j["site"] == "flow" for j in _all), \
            f"every queued scene must follow the reel to flow, got {[j['site'] for j in _all]}"
        assert fst.project_site("failover-proj") == "flow", "the reel's lock is now flow"
        # ...but once a clip exists the lock is absolute: flow dry, krea healthy -> still WAIT
        fbr.hit_wall("flow", "f1", "block")
        assert runner.process_job(fst, fbr, fst.next_queued("failover-proj"), headless=True) == "waiting", \
            "a reel already shot on flow must WAIT for flow, never jump to krea"
        print("OK: whole-reel failover — a dry primary moves the whole reel before the first "
              "clip; after one clip the lock is absolute")

        # A COOLING BID IS NOT A DEAD BID (owner, 2026-10-10). The failover above is for a primary
        # that is genuinely OUT. One that merely stepped back — still funded, back in two minutes —
        # must be waited for. A soft `want_site` used to fall straight through to the next site:
        # a single `selector` wall moved a 12-credit Google Vids reel onto vibes, which is silent
        # and 464x832, and the reel came back with no narration and its dialogue burned in.
        bst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        bbr = Brain(bst)
        bst.set_credits("googlevids", "g1", remaining=12)   # funded — cooling, not dead
        bst.set_credits("flow", "f1", remaining=8)          # healthy lower tier: must NOT be taken
        bbr.hit_wall("googlevids", "g1", "selector")        # the 120s wall that caused this
        bst.add_job("cooling-proj", 0, "a scene", site="googlevids")
        assert runner.process_job(bst, bbr, bst.next_queued("cooling-proj"), headless=True) == "waiting", \
            "a funded primary in a short cooldown must be waited for, not abandoned"
        assert bst.project_site("cooling-proj") is None, \
            "a cooling primary must not move the reel to a lower tier"
        bst.db.execute("UPDATE lessons SET cooldown_until=0"); bst.db.commit()
        runner._adapter = lambda site: fake    # the primary works again — it was only cooling
        assert runner.process_job(bst, bbr, bst.next_queued("cooling-proj"), headless=True) == "done", \
            "the wait must end the moment the primary is back"
        assert bst.project_clips("cooling-proj")[0]["site"] == "googlevids", \
            "the reel must land on its instructed primary, never the fallback"
        # ...but a site that is DRY or hard-BLOCKED is a real dead end and still fails over.
        bst.set_credits("googlevids", "g1", remaining=0)
        assert not bbr.wait_for_bid("googlevids"), "a dry primary must not be waited for"
        bst.set_credits("googlevids", "g1", remaining=12)
        bbr.hit_wall("googlevids", "g1", "block")
        assert not bbr.wait_for_bid("googlevids"), "a hard-blocked primary must fail over"
        print("OK: a cooling but funded primary is WAITED for; only a dry or blocked one "
              "fails the reel over")

        # a broken balance reader must never block a job or clobber the last known number
        fake.read_credits = lambda s: (_ for _ in ()).throw(RuntimeError("no balance on this page"))
        st.add_job("selftest", 1, "a second blue clip")
        assert runner.process_job(st, br, st.next_queued(), headless=True) == "done", \
            "a failed credit read must not fail the job"
        row = st.db.execute("SELECT remaining, source FROM credits WHERE site='kling'").fetchone()
        assert row["remaining"] == 22 and row["source"] == "observed", \
            f"unknown read keeps the last number and only spends: {dict(row)}"

        # the craft gate makes LIVE vision calls, but this self-check promises no network — stub
        # the gateway and the style pre-flight, then prove the pass ASSEMBLES a finished reel
        # (cards placed, score bed, verified) and that the gate records a real verdict for it.
        import json, vision, agents as _agents
        os.environ["STYLE_CHECK"] = "0"
        os.environ["PUBLISH_LLM"] = "0"     # offline: no LLM for the publish sheet
        _orig_review, _orig_medium = vision.review_frame, vision.medium_of
        _orig_judge = _agents.judge_captions
        vision.review_frame = lambda png: []
        vision.medium_of = lambda png: "3d"
        # the caption-wording check is a live LLM judgement too (qc.check_caption_wording ->
        # agents.judge_captions): stub it here for the same reason as the vision gateway, or the
        # no-network promise is broken and a model's opinion decides the fixture's verdict.
        _agents.judge_captions = lambda script, caps: {"problems": [], "captions": caps}
        # the engine authors these at kickoff (pipeline.py, agent 3); the assembler reads them
        _pd = os.path.join(runner.PROJECTS_DIR, "selftest")
        os.makedirs(_pd, exist_ok=True)
        with open(os.path.join(_pd, "captions.json"), "w", encoding="utf-8") as f:
            json.dump({"font": "rounded",
                       "hook": {"lines": ["वो क्यों चुप थे?"]},
                       "moral": {"lines": ["वक़्त देना ही", "असली तोहफ़ा है"]},
                       "cta": {"lines": ["Standard Sunrise Foundation"]}}, f)
        with open(os.path.join(_pd, "identity.json"), "w", encoding="utf-8") as f:
            json.dump({"reel": "selftest", "brand": "standardsunrisefoundation_ngo",
                       "opener": "self-check", "visual_style": "soft stylized 3-D animation",
                       "transitions": "dissolves", "caption_style": "card", "font": "rounded",
                       "narration": "in-scene", "structure": "a->b", "ending": "hold",
                       "moral": "time is the gift", "motif": "a blue frame"}, f)
        # the bed pipeline.py installs at kickoff (music.choose(..., project_dir=...))
        import subprocess as _sp
        _sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:d=20",
                 "-c:a", "libmp3lame", os.path.join(_pd, "music_raw.mp3")],
                capture_output=True, check=True)
        # ...and the kickoff artifacts the engine authors beside it: agent 1's script, agent 2's
        # shot list, and music.install's provenance row. A reel the pipeline BUILT carries all
        # three, and qc gates go_live on the folder being complete (reel-folder) — a fixture that
        # skips them is not the folder a finished reel has.
        with open(os.path.join(_pd, "script.md"), "w", encoding="utf-8") as f:
            f.write("# selftest\n\nHOOK: वो क्यों चुप थे?\nMORAL: वक़्त देना ही असली तोहफ़ा है\n")
        with open(os.path.join(_pd, "shotlist.json"), "w", encoding="utf-8") as f:
            json.dump({"title": "selftest", "world": "a warm living room",
                       "style": "soft stylized 3-D animation", "music_mood": "warm",
                       "shots": [{"line": "वो क्यों चुप थे?"}]}, f, ensure_ascii=False)
        with open(os.path.join(_pd, "music_meta.json"), "w", encoding="utf-8") as f:
            json.dump({"level_start": 0.0, "mean_db": -21.0, "file": "music_raw.mp3",
                       "title": "self-check bed", "licence": "self-check"}, f)
        try:
            runner.stitch_done_projects(st)
        finally:
            vision.review_frame, vision.medium_of = _orig_review, _orig_medium
            _agents.judge_captions = _orig_judge
        final = os.path.join(_pd, "final_veo.mp4")
        assert os.path.exists(final), f"the pass must assemble {final}"
        import assemble as _asm
        assert not _asm.verify_playable(final, _asm._dur(final)), "the assembled reel must play"
        assert sorted(f for f in os.listdir(_pd)
                      if f.startswith("caption_") and f.endswith(".png")) == \
            ["caption_cta.png", "caption_hook.png", "caption_moral.png"], \
            "the assembler must place all three cards"
        q = st.qc_verdicts("selftest")
        assert q, "the craft gate must persist its verdict for the assembled reel"
        assert q[0]["ok"] == 1, \
            "an assembled reel with cards, a score and audible clips must PASS the gate: " \
            + q[0]["verdict"]
        print("OK: clips done -> ASSEMBLED a reel that plays, cards placed, gate verdict PASS")

        # resilience of the pass itself (it crashed the fleet once on a stale row):
        #  - a project whose clip files are GONE must be skipped, not raise
        #  - a project already finished must be left alone (no second cut, no verdict)
        #  - a project with NO captions.json must still assemble, and the gate must HELD it
        #    for the missing craft layer rather than wave it through
        ghost_id = st.add_job("ghost", 0, "a clip whose file was cleaned up")
        st.set_status(ghost_id, "done", video_path=os.path.join(_tmp, "clips", "ghost", "gone.mp4"))
        craft = os.path.join(runner.PROJECTS_DIR, "crafty")
        os.makedirs(craft, exist_ok=True)
        with open(os.path.join(craft, "final_veo.mp4"), "wb") as f:
            f.write(b"owner's finished reel")
        craft_id = st.add_job("crafty", 0, "already assembled")
        st.set_status(craft_id, "done", video_path=os.path.join(_tmp, "clips", "crafty", "x.mp4"))
        bare = os.path.join(_tmp, "clips", "nocaps")
        os.makedirs(bare, exist_ok=True)
        for i in range(2):
            # moving picture + audio: a solid colour is a frozen still and the verify gate
            # (rightly) refuses to call it a reel
            _sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=6",
                     "-f", "lavfi", "-i", "sine=frequency=440:d=6",
                     "-pix_fmt", "yuv420p", "-shortest",
                     os.path.join(bare, f"clip_{i:03d}.mp4")], capture_output=True, check=True)
        nid = st.add_job("nocaps", 0, "assembled with no caption cards")
        st.set_status(nid, "done", video_path=os.path.join(bare, "clip_000.mp4"))
        nid2 = st.add_job("nocaps", 1, "second clip")
        st.set_status(nid2, "done", video_path=os.path.join(bare, "clip_001.mp4"))

        os.environ["STYLE_CHECK"] = "0"
        os.environ["PUBLISH_LLM"] = "0"     # offline: no LLM for the publish sheet
        vision.review_frame = lambda png: []
        vision.medium_of = lambda png: "3d"
        _agents.judge_captions = lambda script, caps: {"problems": [], "captions": caps}
        try:
            runner.stitch_done_projects(st)           # must not raise on any of these rows
        finally:
            vision.review_frame, vision.medium_of = _orig_review, _orig_medium
            _agents.judge_captions = _orig_judge
        assert not os.path.exists(os.path.join(runner.PROJECTS_DIR, "ghost", "final_veo.mp4")), \
            "a project with missing clips must be skipped"
        assert open(os.path.join(craft, "final_veo.mp4"), "rb").read() == b"owner's finished reel", \
            "a finished cut must NEVER be overwritten"
        assert not st.qc_verdicts("crafty") and not st.qc_verdicts("ghost"), \
            "skipped projects must not get a (misleading) gate verdict"
        assert os.path.exists(final), "the good project must still assemble after the bad ones"
        nq = st.qc_verdicts("nocaps")
        assert nq and nq[0]["ok"] == 0, "a reel with no caption cards must be HELD"
        assert any("no caption cards" in b for b in json.loads(nq[0]["verdict"])["block"]), \
            "the HELD reason must name the missing craft layer"
        # A FINDING MUST LEAVE SOMETHING BEHIND, or the next reel makes it again. It files a
        # CANDIDATE (brain proposes, owner vets), keyed by the CHECK — so three broken layers on
        # one reel are one row, and the same defect across ten reels is hits=10, not ten rows.
        _c = {c["slug"]: c for c in st.candidates()}
        assert "defect-craft-layers" in _c, \
            f"a gate finding must be filed as a candidate: {sorted(_c)}"
        assert "no caption cards" in _c["defect-craft-layers"]["evidence"], \
            "the candidate must carry the finding itself, not just the check's name"
        runner._file_findings(st, {"reel": "nocaps", "block": ["[craft-layers] no caption cards"],
                                   "gate": [], "warn": [], "reject": []})
        assert {c["slug"]: c["hits"] for c in st.candidates()}["defect-craft-layers"] >= 2, \
            "a RECURRING defect must accumulate on one candidate, not scatter near-duplicates"
        # …and the brain's record of this generation must carry THIS run's verdict. _absorb reads
        # the gate's verdict, so running it first memorised the PREVIOUS one (every reel's brain
        # record lagged a generation) — the order in _gate is the fix.
        _gen = [json.loads(e["detail"]) for e in st.events() if e["kind"] == "generation"
                and json.loads(e["detail"]).get("reel") == "nocaps"]
        assert _gen and _gen[-1]["qc"] == 0, \
            f"the generation fact must record THIS run's QC result: {_gen[-1] if _gen else None}"
        print("OK: a gate finding is filed as a candidate keyed by the check, and the brain's "
              "generation fact carries this run's verdict (not the previous one)")
        print("OK: missing clips skipped, a finished cut untouched, an uncaptioned reel HELD")

        # THE RE-SHOOT LOOP — what turns "the gate found it" into "the clip is re-made", and the
        # cap that stops that being an infinite loop. Every part of it is load-bearing: the named
        # clip goes back carrying the reason, ONLY that clip goes back, and the rejected cut MOVES
        # (leave it in place and `stitch_done_projects` skips the project forever, so the re-shot
        # clip is never assembled — the same livelock wearing a different hat).
        reg = os.path.join(runner.PROJECTS_DIR, "regentest")
        os.makedirs(reg, exist_ok=True)
        _rids = [st.add_job("regentest", i, f"original prompt {i}") for i in range(2)]
        for i, _rid in enumerate(_rids):
            st.set_status(_rid, "done",
                          video_path=os.path.join(_tmp, "clips", "regentest", f"clip_{i:03d}.mp4"))
        with open(os.path.join(reg, "final_veo.mp4"), "wb") as f:
            f.write(b"the cut the gate rejected")
        assert runner._regenerate(st, reg, [1], "does not match the reel's look") == [1]
        _rows = {c["idx"]: c for c in st.project_clips("regentest")}
        assert _rows[1]["status"] == "queued" and _rows[0]["status"] == "done", \
            "ONLY the named clip goes back — re-shooting the whole reel is not a fix"
        assert "RE-SHOOT" in _rows[1]["prompt"] and "does not match" in _rows[1]["prompt"], \
            f"the reason must reach the prompt: {_rows[1]['prompt']}"
        assert "RE-SHOOT" not in _rows[0]["prompt"], "an innocent clip's prompt is untouched"
        assert not os.path.exists(os.path.join(reg, "final_veo.mp4")), \
            "the rejected cut must MOVE, or the stitch pass skips this project forever"
        assert glob.glob(os.path.join(reg, "rejected", "*-final_veo.mp4")), \
            "…and it is kept, not deleted: it is the evidence for the re-shoot"
        # the note is appended ONCE, and the loop STOPS at the cap instead of re-rolling forever
        st.set_status(_rids[1], "done")
        runner._regenerate(st, reg, [1], "still wrong")
        assert st.project_clips("regentest")[1]["prompt"].count("RE-SHOOT") == 1, \
            "the note is appended once, not once per attempt"
        st.set_status(_rids[1], "done")
        assert runner._regenerate(st, reg, [1], "still wrong again") == [], \
            f"after {runner.REQUEUE_CAP} re-shoots the loop must STOP — this is the livelock"
        assert st.project_clips("regentest")[1]["status"] == "done", \
            "a clip at the cap is reported, never silently re-rolled again"
        # …and stopping loudly means a CRISIS: the record a person needs, and the check the
        # machine should have had. A stop nobody hears is the same as a hang.
        _cr = crisis.open_crises()
        assert len(_cr) == 1 and _cr[0][1]["kind"] == "requeue-capped", _cr
        assert "still wrong again" in _cr[0][1]["detail"], _cr[0][1]
        assert {c["slug"] for c in st.candidates()} >= {"missing-check-preflight-style-uniform"}, \
            "the cap must file the check that would have caught it, keyed by that check"

        # A WHOLESALE PROMPT REPLACEMENT is a new hypothesis, not the same rejection again, so it
        # must not spend the old budget. The cap exists to stop ONE feedback looping; when the
        # code itself changed what the site is told (2026-10-22: the disaster-shot CARTOON lead,
        # after two identical photoreal rejections), counting it against the old fix would cap a
        # repair that has not been tried. An APPEND — the 'RE-SHOOT —' note — keeps the count.
        _hyp = os.path.join(runner.PROJECTS_DIR, "hypthetest")
        os.makedirs(_hyp, exist_ok=True)
        _hid = st.add_job("hypthetest", 0, "the prompt that produced the defect")
        st.set_status(_hid, "done", video_path=os.path.join(_tmp, "clips", "hypthetest", "clip_000.mp4"))
        st.db.execute("UPDATE jobs SET requeues=? WHERE id=?", (runner.REQUEUE_CAP, _hid))
        st.db.commit()
        assert runner._regenerate(st, _hyp, [0], "the same rejection, same fix") == [], \
            "an APPENDED note is the same hypothesis — it must still hit the cap"
        _hrow = st.project_clips("hypthetest")[0]
        assert _hrow["requeues"] == runner.REQUEUE_CAP, \
            f"the same hypothesis must keep its count, got {_hrow['requeues']}"
        st.set_status(_hid, "done")
        st.db.execute("UPDATE jobs SET requeues=? WHERE id=?", (runner.REQUEUE_CAP, _hid))
        st.db.commit()
        _new = "REQUIRED OUTPUT: PIXAR-STYLE CARTOON 3-D — a different prompt entirely"
        st.set_status(_hid, "done")
        st.requeue("hypthetest", [0], prompt=_new)
        _hrow2 = st.project_clips("hypthetest")[0]
        assert _hrow2["requeues"] == runner.REQUEUE_CAP - 1, \
            f"a wholesale prompt replacement must give the cap its budget back, got " \
            f"{_hrow2['requeues']}"
        assert _hrow2["prompt"] == _new, "the new prompt must be the one queued"

        # A CLIP OUT OF RETRIES is the other end of the same rule: its job goes to 'failed', and a
        # 'failed' job is a reel that can never assemble — while the reel is locked to ONE site, so
        # there is no other site to try. That is a person's, and it must not be just a row.
        st.set_credits("kling", "a1", remaining=100)
        cid = st.add_job("crisistest", 0, "a clip that never lands")
        st.db.execute("UPDATE jobs SET retries=? WHERE id=?", (runner.MAX_RETRIES - 1, cid))
        st.db.execute("UPDATE lessons SET cooldown_until=0")     # the site is available again
        st.db.commit()
        calls["n"] = 0                                           # the adapter walls on the poll
        _cjob = st.db.execute("SELECT * FROM jobs WHERE id=?", (cid,)).fetchone()
        assert runner.process_job(st, br, _cjob, headless=True) == "walled"
        assert st.db.execute("SELECT status FROM jobs WHERE id=?", (cid,)).fetchone()[0] == "failed"
        _c = [r for _, r in crisis.open_crises() if r["kind"] == "clip-failed"]
        assert _c and _c[0]["subject"] == "crisistest", crisis.open_crises()
        assert "one reel, one source" in " ".join(_c[0]["tried"]), \
            "the record must say why no other site was tried: the reel is locked to one"
        print("OK: a gate finding requeues the named clip with the reason, moves the rejected cut, "
              "and STOPS at the cap instead of looping")

        # THE FIRST LOOK (owner fix, 2026-10-14). Every craft check runs on the FINISHED reel, so a
        # source that renders this reel photoreal is found four to six generations later — all of
        # them paid for. The reel's OWN first clip is the cheap tell: it is judged by qc's own
        # medium check, the rejection goes into clip 0's prompt as a re-shoot, and when that budget
        # is spent the rest of the reel is PARKED instead of rendered into a film that cannot
        # assemble.
        import subprocess
        import qc as _q
        est = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        ebr = Brain(est)
        est.set_credits("googlevids", "g1", remaining=20)
        bought = []
        elook = types.SimpleNamespace(name="googlevids", credit_cost=1)
        elook.submit = lambda s, prompt: bought.append(prompt)
        elook.poll = lambda s: "done"
        def elook_dl(s, dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=720x1280:rate=24:duration=4",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=4",
                            "-pix_fmt", "yuv420p", "-shortest", dest],
                           capture_output=True, check=True)
            return dest
        elook.download = elook_dl
        runner._adapter = lambda site: elook
        _q._CLIPS_ROOT = runner.CLIPS_DIR              # the clips this test just wrote
        vision.medium_of = lambda png: "photoreal"     # the source ignored the 3-D requirement
        for i in range(3):
            est.add_job("firstlook", i, f"scene {i}", site="googlevids")
        e0 = est.db.execute("SELECT * FROM jobs WHERE project='firstlook' AND idx=0").fetchone()
        assert runner.process_job(est, ebr, e0, headless=True) == "reshoot"
        r0 = est.db.execute("SELECT * FROM jobs WHERE project='firstlook' AND idx=0").fetchone()
        assert r0["status"] == "queued" and r0["requeues"] == 1, dict(r0)
        assert "RE-SHOOT —" in r0["prompt"] and "3-D" in r0["prompt"], \
            "the rejection is folded into the re-shoot's prompt, once"
        assert len(bought) == 1, "the first look must not buy a second clip"
        assert est.project_clips("firstlook")[1]["status"] == "queued", \
            "the reel's other scenes wait while clip 0 is re-shot"
        # …and when the re-shoot budget is spent, the reel STOPS costing generations
        est.db.execute("UPDATE jobs SET requeues=? WHERE project='firstlook' AND idx=0",
                       (runner.REQUEUE_CAP,))
        est.db.commit()
        e0 = est.db.execute("SELECT * FROM jobs WHERE project='firstlook' AND idx=0").fetchone()
        assert runner.process_job(est, ebr, e0, headless=True) == "reshoot"
        statuses = {c["idx"]: c["status"] for c in est.project_clips("firstlook")}
        assert set(statuses.values()) == {"failed"}, \
            f"a reel whose first clip will not come right must be parked, not rendered: {statuses}"
        assert len(bought) == 2, \
            f"it buys exactly one more render, never the other scenes: {len(bought)}"
        print("OK: the reel's FIRST clip is judged before the rest are bought — a photoreal or "
              "frozen opener re-shoots once, then PARKs the reel instead of rendering four more "
              "scenes of a film that can never assemble")

        # NEVER BUY THE SAME RENDER TWICE (owner fix, 2026-10-14). The clip EXISTS on the site the
        # moment the poll says done, so a failed fetch is retried while the page is still open — a
        # wall there requeues the clip and the retry submits a brand-new render for something the
        # site has already made. And a shutdown between the download and the 'done' commit leaves
        # a finished file on disk, which the resume path must KEEP.
        dst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        dbr = Brain(dst)
        vision.medium_of = lambda png: "3d"             # a good clip: the FETCH is what is tested
        dst.set_credits("kling", "a1", remaining=20)
        tried = {"fetch": 0, "submit": 0}
        dad = types.SimpleNamespace(name="kling", credit_cost=1)
        dad.submit = lambda s, prompt: tried.__setitem__("submit", tried["submit"] + 1)
        dad.poll = lambda s: "done"
        def dad_dl(s, dest):
            tried["fetch"] += 1
            if tried["fetch"] < 3:
                raise Wall("selector", "download control not found")
            return elook_dl(s, dest)
        dad.download = dad_dl
        runner._adapter = lambda site: dad
        dst.add_job("fetchtest", 0, "a clip that downloads on the third try", site="kling")
        _dj = dst.db.execute("SELECT * FROM jobs WHERE project='fetchtest'").fetchone()
        assert runner.process_job(dst, dbr, _dj, headless=True) == "done"
        assert tried["fetch"] == 3 and tried["submit"] == 1, \
            f"a flaky fetch must not become a second render: {tried}"
        # the resume rule: a mid-DOWNLOAD row holding a finished clip is kept, a QUEUED row is not
        # (the gate requeues a clip it rejected while the rejected file is still sitting there)
        _kept = os.path.join(runner.CLIPS_DIR, "fetchtest", "clip_000.mp4")
        assert runner._already_downloaded({"project": "fetchtest", "idx": 0,
                                           "status": "downloading"}) == _kept, \
            "a shutdown mid-download leaves a clip worth keeping"
        assert runner._already_downloaded({"project": "fetchtest", "idx": 0,
                                           "status": "queued"}) == "", \
            "a queued clip must be re-shot even though the rejected file is still on disk"
        assert runner._already_downloaded({"project": "fetchtest", "idx": 7,
                                           "status": "downloading"}) == "", \
            "a row with no file on disk has nothing to keep"
        print("OK: a failed fetch is retried in-session (the render already exists), and a clip "
              "already downloaded is never re-submitted")

        # The Chrome-profile pin is written by import_login.py and read here, from two files — so
        # prove the two agree instead of trusting they do. Get the path wrong and a second account
        # re-imports the first one's Google session: it looks signed in, and it hits the same limit.
        import shutil, browser
        _probe = browser.Session("pincheck", "a0").profile
        with open(os.path.join(_probe, ".chrome-profile"), "w") as f:
            f.write("Profile 7\n")
        assert runner._chrome_pin("pincheck", "a0") == "Profile 7", \
            f"import_login writes the pin to {_probe}; the runner must read it from there"
        assert runner._chrome_pin("pincheck", "nosuch") == "", \
            "an account with no recorded profile must fall back to scanning, not raise"
        shutil.rmtree(_probe, ignore_errors=True)
        print("OK: an imported account remembers its Chrome profile, so a second account cannot "
              "silently inherit the first one's session")

        # A REFRESH THAT LANDS ON A LOGIN WALL MUST NOT DESTROY THE WORKING SESSION. The
        # persistent profile IS the account's session, so writing a signed-out cookie set over a
        # working one loses the account (2026-10-22: a1 was signed in at 22:52, the 00:02 refresh
        # injected signed-out cookies, and the reel lost the account that was working). The import
        # now keeps the old jar and puts it back when the new one lands on a login wall.
        import import_login
        _jar = [{"name": "n", "value": "v", "domain": ".google.com", "path": "/"}]
        class _FakeCtx:
            def __init__(self): self.jar = []
            def cookies(self): return list(self.jar)
            def add_cookies(self, cs): self.jar.extend(cs)
            def clear_cookies(self): self.jar = []
        _ctx = _FakeCtx()
        _ctx.jar = list(_jar)
        # the refresh injects a signed-out set, and the landing page says 'login'
        _ctx.add_cookies([{"name": "broken", "value": "x", "domain": ".google.com", "path": "/"}])
        assert import_login._restore_if_worse(_ctx, _jar, "login") == "restored", \
            "a login wall must fall back to the previous session"
        assert [c["name"] for c in _ctx.cookies()] == ["n"], \
            "the restore must put the PREVIOUS session back, not the broken one"
        # a refresh that WORKS is left alone
        _ctx.jar = []
        _ctx.add_cookies([{"name": "fresh", "value": "y", "domain": ".google.com", "path": "/"}])
        assert import_login._restore_if_worse(_ctx, _jar, "done") == "done"
        assert [c["name"] for c in _ctx.cookies()] == ["fresh"], \
            "a working refresh must not be rolled back"
        print("OK: a refresh that lands on a login wall keeps the previous session")

        # THE ACCOUNT IDENTITY CHECK. Three Google accounts share one pool of Chrome profiles,
        # and the failure this prevents is invisible: account 2 holding account 1's session,
        # spending account 1's generation limit, and the per-account credit counts — the whole
        # point of tracking them separately — turning into fiction.
        ist = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        ist.set_pool("googlevids", "a2", 50, email="cherishdhingra1234@gmail.com")

        class _Sess:
            def __init__(self, email): self.email = email
            def __getattr__(self, _k): raise AttributeError
        class _Pg:
            def __init__(self, email): self.email = email
            def evaluate(self, _js): return self.email
        def _session(email):
            s = _Sess(email); s.page = _Pg(email); return s
        class _Ad:
            def read_account(self, session): return session.page.email
        _ad = _Ad()
        # the right account -> it proceeds
        assert runner._check_identity(ist, _ad, _session("cherishdhingra1234@gmail.com"),
                                      "googlevids", "a2") is True, \
            "the session holding the right account must be allowed to spend"
        # the WRONG account -> refused before a credit is spent
        try:
            runner._check_identity(ist, _ad, _session("cherishdhingra12@gmail.com"),
                                   "googlevids", "a2")
            raise AssertionError("a session holding another account must be refused")
        except Wall as w:
            assert w.kind == "login", w.kind
            assert "wrong account" in w.detail, w.detail
        # the page says NOTHING -> unknown is not evidence, so it is allowed through
        assert runner._check_identity(ist, _ad, _session(""), "googlevids", "a2") is True, \
            "a page that does not say which account it is must not be treated as a mismatch"
        # no adapter that can read an account -> the check is skipped, not failed
        assert runner._check_identity(ist, object(), _session("x"), "googlevids", "a2") is True, \
            "a site that cannot name its account must not block the job"
        print("OK: the right account may spend; a session holding ANOTHER account is refused "
              "before the credit is spent; an unknown account is not a mismatch")

        # THE PRE-CHOICE BALANCE SWEEP (owner rule, 2026-10-27): before any reel chooses its
        # source, every choosable account is asked what it ACTUALLY holds, and the site's
        # answer outranks the ledger's guess. What must hold here: a number read is recorded
        # observed; a cooling pair is never visited; a site that states no balance keeps its
        # row; a session holding ANOTHER account records nothing; a crashed read fails open;
        # and a row just observed is not re-read within the sweep's max age.
        sst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        sst.set_credits("flow", "a1", remaining=3)          # the ledger GUESSES 3 (manual)
        sst.set_pool("flow", "a1", 50, email="one@example.com")
        sst.set_credits("flow", "a2", remaining=50)
        sst.record_lesson("flow", "captcha", account="a2", cooldown_s=9999)   # step back
        sst.set_credits("novid", "b1", remaining=7)
        rich = types.SimpleNamespace(name="flow", credit_cost=1, home="https://flow.test")
        rich.read_credits = lambda s: 61
        rich.read_account = lambda s: "one@example.com"
        novid = types.SimpleNamespace(name="novid", credit_cost=1, home="https://novid.test")
        novid.read_credits = lambda s: None                 # the page shows no counter (Vids)
        runner._adapter = lambda site: rich if site == "flow" else novid
        class _GotoSession:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def goto(self, url): pass
        runner._session = lambda site, account, headless: _GotoSession()
        out = runner.sweep_balances(sst, headless=True)
        row = sst.db.execute("SELECT remaining, source FROM credits "
                             "WHERE site='flow' AND account='a1'").fetchone()
        assert row["remaining"] == 61 and row["source"] == "observed", \
            f"the site's own number outranks the ledger's guess: {dict(row)}"
        assert ("flow", "a1", 61) in out["observed"], out
        row = sst.db.execute("SELECT remaining FROM credits "
                             "WHERE site='flow' AND account='a2'").fetchone()
        assert row["remaining"] == 50, "a cooling pair must not be visited at all"
        assert ("flow", "a2") not in [(s, a) for s, a, *_ in
                                      out["observed"] + out["unreadable"] + out["failed"]], out
        row = sst.db.execute("SELECT remaining, source FROM credits "
                             "WHERE site='novid'").fetchone()
        assert row["remaining"] == 7 and row["source"] == "manual", \
            f"a site that states no balance must not have its row rewritten: {dict(row)}"
        # a session holding ANOTHER account: the read is refused, the row survives
        liar = types.SimpleNamespace(name="liar", credit_cost=1, home="https://liar.test")
        liar.read_credits = lambda s: 999
        liar.read_account = lambda s: "someone@else.example.com"
        runner._adapter = lambda site: liar
        sst.set_credits("liar", "a1", remaining=10)
        sst.set_pool("liar", "a1", 50, email="real@example.com")
        out = runner.sweep_balances(sst, headless=True, sites=["liar"])
        row = sst.db.execute("SELECT remaining, source FROM credits WHERE site='liar'").fetchone()
        assert row["remaining"] == 10 and row["source"] == "manual", \
            f"a wrong-account session must not write a balance: {dict(row)}"
        assert out["mismatch"], out
        # a crashed read fails open — never a number, never a stop
        def _boom(s): raise RuntimeError("gateway down")
        liar.read_credits = _boom
        out = runner.sweep_balances(sst, headless=True, sites=["liar"])
        row = sst.db.execute("SELECT remaining FROM credits WHERE site='liar'").fetchone()
        assert row["remaining"] == 10, "a crashed read must not become a number"
        # freshness: a row just observed is not re-read within max_age
        out = runner.sweep_balances(sst, headless=True, sites=["flow"], max_age=10_000)
        assert out["observed"] == [] and out["skipped"], out
        print("OK: the pre-choice sweep records the site's own number (observed > guess), never "
              "visits a cooling pair, never rewrites a no-counter row, refuses a wrong-account "
              "session, fails open on a crash, and skips fresh rows")

        print("OK: hermetic self-check (temp dirs) + craft-gate verdict persisted")
        print("OK: observed balance updates the count before spend; a failed read never blocks a job")
    elif "--sweep" in sys.argv:
        # The standalone form of the sweep the loop runs before any reel chooses its source:
        # ask every choosable site what it holds right now, then print the whole ledger.
        _st = State()
        sweep_balances(_st, headless="--headed" not in sys.argv)
        print("\nthe ledger now believes:")
        for r in _st.db.execute("SELECT site, account FROM credits ORDER BY site, account"):
            print("  " + _st.credit_line(r["site"], r["account"]))
        _st.close()
    else:
        loop(once="--once" in sys.argv, headless="--headed" not in sys.argv)
