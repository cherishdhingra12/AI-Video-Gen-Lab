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
POLL_EVERY = 8.0                             # seconds between poll looks
POLL_TIMEOUT = 12 * 60                       # give a render up to 12 min


def _adapter(site: str):
    return importlib.import_module(f"sites.{site}").ADAPTER


def _session(site: str, account: str, headless: bool):
    from browser import Session
    return Session(site, account, headless=headless)


def _refresh_cookies(site: str, account: str, keywords) -> None:
    """Re-hydrate a profile from real Chrome right before a run. Some sites (Google Flow)
    rotate session cookies fast, so an old snapshot import signs out within minutes; a fresh
    pull each run keeps them live. Best-effort — a still-signed-out state is caught by the
    adapter's own login Wall. READ-only on the real browser (see import_login.py)."""
    import subprocess
    cmd = [sys.executable, os.path.join(_DIR, "import_login.py"), site, account, "--browser", "chrome"]
    for kw in keywords:
        cmd += ["--keyword", kw]
    try:
        subprocess.run(cmd, cwd=_DIR, timeout=90, capture_output=True)
    except Exception as e:
        print(f"  cookie refresh for {site}/{account} skipped: {e}")


def _observe_credits(st: State, adapter, session, site: str, account: str) -> None:
    """Best-effort: read this account's REAL balance off the page we're already on, before the
    local spend, so the scheduler and brain.report work from observed numbers instead of the
    hand-entered signup guess. Fail-open — an unclear read (None / nonsense / gateway down)
    keeps the last known number and never blocks the job."""
    reader = getattr(adapter, "read_credits", None)
    if reader is None:
        return
    try:
        n = reader(session)
    except Exception as e:
        print(f"  credit read skipped for {site}/{account}: {str(e)[:120]}")
        return
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return                                  # not clear -> keep the last known number
    st.observe_credits(site, account, n)
    print(f"  {site}/{account}: observed {n} credits on the site")


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


def process_job(st: State, br: Brain, job, headless: bool = True) -> str:
    """Drive one clip end to end. Returns final status. Any Wall -> cool down +
    requeue (not a failure). Each step commits before the next, so a crash resumes."""
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
        if not pick:
            return "waiting"
        site = pick[0]
    else:
        if br.wait_for_bid(job["site"]):
            # ponytail: waiting breaks the whole queue loop, so a parked reel stalls other
            # projects' queued jobs too. Fine while one reel is in flight at a time (how this
            # house runs); skip-to-the-next-project if a second reel is ever queued alongside.
            return "waiting"                               # a short cooldown is not a dead end
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
            _observe_credits(st, adapter, s, site, account)   # observed > hand-entered
            st.spend_credit(site, account, adapter.credit_cost)

            deadline = time.time() + POLL_TIMEOUT
            while time.time() < deadline:
                state = adapter.poll(s)
                if state == "done":
                    st.set_status(job["id"], "downloading")
                    adapter.download(s, dest)
                    st.set_status(job["id"], "done", video_path=dest)
                    br.note_success(site, account)
                    return "done"
                if state in ("captcha", "rate_limited", "login", "error"):
                    kind = {"rate_limited": "rate_limit"}.get(state, state)
                    raise Wall(kind, f"seen while polling ({state})")
                time.sleep(POLL_EVERY)
            raise Wall("block", "render timed out")

    except Wall as w:
        if w.kind == "credits":
            # The site said it is OUT. That is a fact about this account, not a cooldown — record
            # it in the ledger with whatever refill time the site named, so the scheduler stops
            # choosing it and the report can say when it comes back. Without this the balance sat
            # at its hand-entered guess (or at 0 with no date) and the same empty site was picked
            # again and again.
            back = learn.refill_at(w.refill)
            st.mark_dry(site, account, back)
            print(f"  {site}/{account}: out of credits — dry until "
                  + (time.strftime("%Y-%m-%d %H:%M", time.localtime(back)) if back else "a person says otherwise"))
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
        if any(os.path.exists(os.path.join(pdir, f)) for f in qc.Reel.FINALS):
            continue                                  # already finished — never overwrite a cut
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


def loop(once: bool = False, headless: bool = True):
    st, br = State(), Brain()
    # 0) prime the brain: re-seed its canonical lessons so one added to qc.LESSONS since the last
    # run reaches THIS one (the pre-generation trigger, at the driving end). Fail-open.
    try:
        import qc as _qc
        _qc.seed(st)
    except Exception as e:  # noqa: BLE001
        print(f"  brain preflight skipped ({str(e)[:100]})")
    # 1) resume anything caught mid-flight by a shutdown — as if it never happened
    for job in st.resume():
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
            # (WAVELENGTH.md §3), and a silent clip is a defect the assembler must reject.
            subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=blue:s=720x1280:d=6",
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
        assert runner.process_job(cst, cbr, _dj, headless=True) == "walled"
        row = cst.db.execute(
            "SELECT remaining, resets_at, source FROM credits WHERE site='flow'").fetchone()
        assert row["remaining"] == 0 and row["source"] == "observed", dict(row)
        assert row["resets_at"] > time.time(), "the refill time the site named is in the ledger now"
        assert ("flow", "f1") not in {(c["site"], c["account"]) for c in cst.available()}, \
            "an empty site must not be offered again"
        assert cbr.wait_for_bid("flow") is False, "an empty site is a dead end, not a wait"
        assert cbr.pick(want_site="flow") == ("krea", "k1"), \
            "the reel moves to a source that has credit"
        print("OK: a site that says it is out of credits is marked dry with its own refill time")

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

        # WHOLE-REEL FAILOVER (owner, 2026-10-10): before ANY clip lands, a dry primary moves the
        # ENTIRE reel to the next waterfall site, and every queued scene follows. Once a clip is
        # done, project_site() owns the lock and nothing may move the reel again.
        fst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        fbr = Brain(fst)
        fst.set_credits("googlevids", "g1", remaining=0)    # primary dry from the start
        fst.set_credits("flow", "f1", remaining=8)          # next in waterfall
        runner._adapter = lambda site: fake
        runner._session = lambda site, account, headless: _NullSession()
        fake2 = types.SimpleNamespace(name="flow", credit_cost=2)
        fake2.submit = lambda s, prompt: None
        fake2.poll = lambda s: "done"
        def fake2_dl(s, dest):
            import subprocess as _sp
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _sp.run(["ffmpeg","-y","-f","lavfi","-i","color=c=green:s=720x1280:d=4",
                     "-f","lavfi","-i","sine=frequency=440:d=4",
                     "-pix_fmt","yuv420p","-shortest",dest],
                    capture_output=True, check=True)
        fake2.download = fake2_dl
        fake2.read_credits = lambda s: None
        fjid = fst.add_job("failover-proj", 0, "a scene", site="googlevids")
        fst.add_job("failover-proj", 1, "another scene", site="googlevids")
        runner._adapter = lambda site: fake if site != "flow" else fake2
        assert runner.process_job(fst, fbr, fst.next_queued("failover-proj"), headless=True) == "done", \
            "a dry primary must fail the WHOLE reel over to flow"
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
        print("OK: hermetic self-check (temp dirs) + craft-gate verdict persisted")
        print("OK: observed balance updates the count before spend; a failed read never blocks a job")
    else:
        loop(once="--once" in sys.argv, headless="--headed" not in sys.argv)
