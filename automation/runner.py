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
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "sites"))
sys.path.insert(0, os.path.dirname(_DIR))       # project root, for stitch.py

from state import State                     # noqa: E402
from learn import Brain                      # noqa: E402
from base import Wall                        # noqa: E402  (same module identity as adapters' `from base import Wall`)

ROOT = os.path.dirname(_DIR)
CLIPS_DIR = os.path.join(ROOT, "clips")            # per-project downloaded clips
PROJECTS_DIR = os.path.join(ROOT, "projects")      # finished reels land here (final.mp4)
MAX_RETRIES = 3
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


def process_job(st: State, br: Brain, job, headless: bool = True) -> str:
    """Drive one clip end to end. Returns final status. Any Wall -> cool down +
    requeue (not a failure). Each step commits before the next, so a crash resumes."""
    # One reel, one source (the owner's rule, 2026-10-09). Once ANY clip of this project has come
    # back, that site is the reel's lock and nothing else may add a scene to it — pick(strict=True)
    # returns None while it is cooling, so the loop WAITS for the primary instead of filling in from
    # a fallback (this reel first ended up krea+vibes+googlevids, three looks in one cut).
    # BEFORE the first clip lands the job's `site` is only an opening bid: if that bid is unusable
    # (dry / disabled / cooling for good), the WHOLE reel fails over to the next priority site
    # (whole-reel fails over, never a scene at a time — see SITE_ORDER in learn.py).
    locked = st.project_site(job["project"])
    if locked:
        pick = br.pick(want_site=locked, strict=True)      # a reel in progress waits, never mixes
        if not pick:
            return "waiting"
        site = pick[0]
    else:
        pick = br.pick(want_site=job["site"], strict=False)
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
        cd = br.hit_wall(site, account, w.kind, step=job["status"], detail=w.detail)
        n = st.bump_retry(job["id"])
        # step back: requeue unless we've exhausted retries on this clip
        st.set_status(job["id"], "queued" if n < MAX_RETRIES else "failed",
                      error=f"{w.kind}: {w.detail} (cooldown {cd:.0f}s)")
        print(f"  wall on {site}/{account}: {w.kind} -> cool {cd:.0f}s, retry {n}/{MAX_RETRIES}")
        return "walled"
    except Exception as e:                          # unexpected -> record, requeue
        n = st.bump_retry(job["id"])
        st.set_status(job["id"], "queued" if n < MAX_RETRIES else "failed", error=str(e)[:300])
        print(f"  error on {site}/{account}: {e}")
        return "error"


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
    Every verdict is followed by _absorb, so a reel that PASSES teaches the brain as much as
    one that fails — the loop runs on every generation, not only on problems."""
    _absorb(project_dir, st)
    try:
        import qc
        v = qc.run(project_dir, st)
        for b in v["block"]:
            print(f"  QC BLOCK: {b}")
        for g in v["gate"]:
            print(f"  QC PUBLISH-GATE: {g}")
        print(f"  QC {project_dir}: {'PASS' if v['ok'] else 'HELD'} "
              f"({len(v['block'])} block, {len(v['gate'])} gate, {len(v['checklist'])} to eyeball) "
              "— verdict saved to state.db")
    except Exception as e:
        print(f"  QC skipped ({e})")


def loop(once: bool = False, headless: bool = True):
    st, br = State(), Brain()
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
        import json, vision
        os.environ["STYLE_CHECK"] = "0"
        _orig_review, _orig_medium = vision.review_frame, vision.medium_of
        vision.review_frame = lambda png: []
        vision.medium_of = lambda png: "3d"
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
        final = os.path.join(_pd, "final_veo.mp4")
        assert os.path.exists(final), f"the pass must assemble {final}"
        import assemble as _asm
        assert not _asm.verify_playable(final, _asm._dur(final)), "the assembled reel must play"
        assert sorted(f for f in os.listdir(_pd) if f.startswith("caption_")) == \
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
        try:
            runner.stitch_done_projects(st)           # must not raise on any of these rows
        finally:
            vision.review_frame, vision.medium_of = _orig_review, _orig_medium
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
        print("OK: missing clips skipped, a finished cut untouched, an uncaptioned reel HELD")
        print("OK: hermetic self-check (temp dirs) + craft-gate verdict persisted")
        print("OK: observed balance updates the count before spend; a failed read never blocks a job")
    else:
        loop(once="--once" in sys.argv, headless="--headed" not in sys.argv)
