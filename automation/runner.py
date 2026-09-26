"""
runner.py — the loop. Reads the durable brain, drives one clip at a time through
the best available site, learns from every wall, and stitches finished projects.

Crash-safe by construction: every state change is committed to SQLite before the
next step, so a shutdown at any point leaves a row the next boot picks up. Start it
from launchd on wake and it continues "as if the shutdown never happened":

    resume mid-flight jobs first  ->  then drain the queue  ->  stitch done projects

    python3 runner.py                 # run the loop once (launchd calls this)
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
CLIPS_DIR = os.path.join(ROOT, "clips")      # per-project downloaded clips
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


def process_job(st: State, br: Brain, job, headless: bool = True) -> str:
    """Drive one clip end to end. Returns final status. Any Wall -> cool down +
    requeue (not a failure). Each step commits before the next, so a crash resumes."""
    pick = br.pick(want_site=job["site"])          # honor prior routing if set
    if not pick:
        return "waiting"                            # nothing attackable right now
    site, account = pick
    adapter = _adapter(site)
    dest = os.path.join(CLIPS_DIR, job["project"], f"clip_{job['idx']:03d}.mp4")

    st.set_status(job["id"], "submitting", site=site, account=account)
    try:
        if getattr(adapter, "needs_cookie_refresh", False):
            _refresh_cookies(site, account, getattr(adapter, "cookie_keywords", [site]))
        with _session(site, account, headless) as s:
            adapter.submit(s, job["prompt"])
            st.set_status(job["id"], "rendering")
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
    """Any project whose every clip is done -> stitch into final.mp4 (once)."""
    import stitch as stitcher
    seen = {r["project"] for r in st.db.execute("SELECT DISTINCT project FROM jobs")}
    for project in seen:
        clips = st.project_clips(project)
        if clips and all(c["status"] == "done" for c in clips):
            final = os.path.join(ROOT, "projects", project, "final.mp4")
            if os.path.exists(final):
                continue
            os.makedirs(os.path.dirname(final), exist_ok=True)
            paths = [c["video_path"] for c in clips]
            stitcher.stitch(paths, final, xfade=0.4)
            print(f"STITCHED {project} -> {final} ({len(paths)} clips)")


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
        if r == "waiting":                           # all sites cooling/dry -> stop for now
            print("all sites cooling down or out of credit; will resume next run.")
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
        def fake_submit(s, prompt): calls["n"] += 1
        def fake_poll(s):
            return "captcha" if calls["n"] == 1 else "done"
        def fake_download(s, dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            import subprocess
            subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=blue:s=64x64:d=1",
                            "-pix_fmt","yuv420p",dest], capture_output=True, check=True)
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

        jid = st.add_job("selftest", 0, "a blue clip")
        # 1st pass: hits the captcha wall -> requeued + cooled
        assert runner.process_job(st, br, st.next_queued(), headless=True) == "walled"
        assert st.cooldown_left("kling", "a1") > 0, "wall must cool the site"
        # clear cooldown to simulate 'came back later', 2nd pass: succeeds
        st.db.execute("UPDATE lessons SET cooldown_until=0"); st.db.commit()
        assert runner.process_job(st, br, st.next_queued(), headless=True) == "done"
        clip = st.project_clips("selftest")[0]
        assert clip["status"] == "done" and os.path.exists(clip["video_path"])
        runner.stitch_done_projects(st)
        assert os.path.exists(os.path.join(ROOT, "projects", "selftest", "final.mp4"))
        print("OK: loop walls->cools->requeues, then succeeds->done->stitches final.mp4")
    else:
        loop(once="--once" in sys.argv, headless="--headed" not in sys.argv)
