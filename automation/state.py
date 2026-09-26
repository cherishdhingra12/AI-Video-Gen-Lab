"""
state.py — the durable brain. One SQLite file = every fact the engine needs to
survive a shutdown. Wake up, read this, continue as if nothing happened.

Three tables:
  jobs     one row per CLIP to make. status walks
           queued -> submitting -> rendering -> downloading -> done | failed.
           A crash mid-step leaves the row in that step; resume() re-drives it.
  credits  per (site, account): how many free credits are left and when they reset.
           The scheduler reads this to pick where to send the next clip.
  lessons  what went wrong before, so we never repeat it (anti-ban). A wall
           (captcha/rate-limit) writes a cooldown_until on (site, account);
           the scheduler skips that pair until the clock passes = step back,
           then come back and attack again.

WAL mode + one connection = a power-cut mid-write can't corrupt the file
(same restart discipline as the trading systems). Everything is a committed txn.

    st = State()                      # opens/creates ~/…/state.db
    jid = st.add_job(project, idx, prompt)
    st.set_status(jid, "rendering", site="kling", account="a1")
    for j in st.resume(): ...         # rows stuck mid-flight after a restart
"""

import os
import sqlite3
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(_DIR), "state.db")

# steps that mean "in flight" — a restart must pick these back up
LIVE = ("submitting", "rendering", "downloading")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  project   TEXT NOT NULL,
  idx       INTEGER NOT NULL,        -- clip order within the project
  prompt    TEXT NOT NULL,
  first_png TEXT,                    -- optional: path to a start frame (chaining)
  status    TEXT NOT NULL DEFAULT 'queued',
  site      TEXT,                    -- where it was last routed
  account   TEXT,
  video_path TEXT,                   -- filled when downloaded
  retries   INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  updated   REAL NOT NULL,
  UNIQUE(project, idx)
);
CREATE TABLE IF NOT EXISTS credits (
  site    TEXT NOT NULL,
  account TEXT NOT NULL,
  remaining INTEGER NOT NULL DEFAULT 0,
  resets_at REAL NOT NULL DEFAULT 0, -- epoch; past = refill is due
  updated REAL NOT NULL,
  PRIMARY KEY (site, account)
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY,
  site    TEXT NOT NULL,
  account TEXT,
  step    TEXT,                      -- where it happened
  kind    TEXT NOT NULL,             -- 'captcha' | 'rate_limit' | 'selector' | ...
  detail  TEXT,                      -- what the screen said / why it failed
  cooldown_until REAL NOT NULL DEFAULT 0,  -- skip (site,account) until this epoch
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_lessons_site ON lessons(site, account);
"""


class State:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")     # survive a mid-write crash
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    # ---- jobs ----
    def add_job(self, project: str, idx: int, prompt: str, first_png: str = None) -> int:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO jobs(project,idx,prompt,first_png,updated) "
            "VALUES(?,?,?,?,?)", (project, idx, prompt, first_png, time.time()))
        self.db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        return self.db.execute(
            "SELECT id FROM jobs WHERE project=? AND idx=?", (project, idx)).fetchone()["id"]

    def set_status(self, job_id: int, status: str, site: str = None,
                   account: str = None, video_path: str = None, error: str = None):
        # only overwrite site/account/etc. when a new value is given (COALESCE)
        self.db.execute(
            "UPDATE jobs SET status=?, "
            "site=COALESCE(?,site), account=COALESCE(?,account), "
            "video_path=COALESCE(?,video_path), last_error=?, updated=? WHERE id=?",
            (status, site, account, video_path, error, time.time(), job_id))
        self.db.commit()

    def bump_retry(self, job_id: int) -> int:
        self.db.execute("UPDATE jobs SET retries=retries+1, updated=? WHERE id=?",
                        (time.time(), job_id))
        self.db.commit()
        return self.db.execute("SELECT retries FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

    def resume(self) -> list:
        """Rows stuck mid-flight after a restart — re-drive these first."""
        q = ",".join("?" * len(LIVE))
        return self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY updated", LIVE).fetchall()

    def next_queued(self, project: str = None) -> sqlite3.Row:
        if project:
            return self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' AND project=? ORDER BY idx LIMIT 1",
                (project,)).fetchone()
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def project_clips(self, project: str) -> list:
        return self.db.execute(
            "SELECT * FROM jobs WHERE project=? ORDER BY idx", (project,)).fetchall()

    # ---- credits ----
    def set_credits(self, site: str, account: str, remaining: int, resets_at: float = 0):
        self.db.execute(
            "INSERT INTO credits(site,account,remaining,resets_at,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(site,account) DO UPDATE SET remaining=excluded.remaining, "
            "resets_at=excluded.resets_at, updated=excluded.updated",
            (site, account, remaining, resets_at, time.time()))
        self.db.commit()

    def spend_credit(self, site: str, account: str, n: int = 1):
        self.db.execute(
            "UPDATE credits SET remaining=MAX(0,remaining-?), updated=? WHERE site=? AND account=?",
            (n, time.time(), site, account))
        self.db.commit()

    def available(self, now: float = None) -> list:
        """(site,account) pairs with credit left and not in cooldown — for the
        scheduler. A pair whose resets_at has passed is treated as refillable
        (remaining is trusted until the runner refreshes it)."""
        now = now or time.time()
        rows = self.db.execute(
            "SELECT c.site, c.account, c.remaining, c.resets_at, "
            " (SELECT MAX(cooldown_until) FROM lessons l "
            "  WHERE l.site=c.site AND (l.account=c.account OR l.account IS NULL)) AS cd "
            "FROM credits c").fetchall()
        out = []
        for r in rows:
            if (r["cd"] or 0) > now:
                continue                       # stepped back, still cooling
            if r["remaining"] > 0 or (r["resets_at"] and r["resets_at"] <= now):
                out.append(r)
        return out

    # ---- lessons (anti-ban) ----
    def record_lesson(self, site: str, kind: str, account: str = None,
                      step: str = None, detail: str = None, cooldown_s: float = 0):
        cd = time.time() + cooldown_s if cooldown_s else 0
        self.db.execute(
            "INSERT INTO lessons(site,account,step,kind,detail,cooldown_until,created) "
            "VALUES(?,?,?,?,?,?,?)", (site, account, step, kind, detail, cd, time.time()))
        self.db.commit()

    def cooldown_left(self, site: str, account: str = None, now: float = None) -> float:
        """Seconds until (site,account) is attackable again. 0 = go now."""
        now = now or time.time()
        row = self.db.execute(
            "SELECT MAX(cooldown_until) AS cd FROM lessons "
            "WHERE site=? AND (account=? OR account IS NULL)", (site, account)).fetchone()
        return max(0.0, (row["cd"] or 0) - now)

    def recent_lessons(self, site: str, account: str = None, kind: str = None,
                       limit: int = 5) -> list:
        """Consult before a risky action: what bit us here before?"""
        sql = "SELECT * FROM lessons WHERE site=?"
        args = [site]
        if account:
            sql += " AND (account=? OR account IS NULL)"; args.append(account)
        if kind:
            sql += " AND kind=?"; args.append(kind)
        sql += " ORDER BY created DESC LIMIT ?"; args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def close(self):
        self.db.close()


# escalating backoff: repeat the same wall -> wait longer each time (step back,
# come back later). Capped so it always eventually retries.
def backoff_seconds(prior_hits: int, base: float = 300.0, cap: float = 6 * 3600.0) -> float:
    return min(cap, base * (2 ** max(0, prior_hits)))


if __name__ == "__main__":
    import tempfile
    st = State(os.path.join(tempfile.mkdtemp(), "t.db"))

    # jobs: add, advance, and prove a mid-flight row is found on "restart"
    j = st.add_job("proj", 0, "a lantern in fog")
    assert st.add_job("proj", 0, "dup") == j, "same (project,idx) must dedupe"
    st.set_status(j, "rendering", site="kling", account="a1")
    assert [r["id"] for r in st.resume()] == [j], "rendering row must be resumable"
    st.set_status(j, "done", video_path="/x/0.mp4")
    assert not st.resume(), "done row must not resume"

    # credits + scheduler availability
    st.set_credits("kling", "a1", remaining=6)
    st.set_credits("veo", "a1", remaining=0, resets_at=time.time() + 9999)  # dry, not due
    avail = {(r["site"], r["account"]) for r in st.available()}
    assert ("kling", "a1") in avail and ("veo", "a1") not in avail, avail
    st.spend_credit("kling", "a1", 6)
    assert not st.available(), "spent-out + not-due -> nothing available"

    # lessons: a wall puts a pair in cooldown; escalation grows the wait
    st.record_lesson("kling", "captcha", account="a1", cooldown_s=300)
    assert st.cooldown_left("kling", "a1") > 250, "captcha must cool the pair down"
    assert backoff_seconds(0) == 300 and backoff_seconds(3) == 2400, "backoff escalates"
    assert st.recent_lessons("kling", "a1", kind="captcha"), "lesson must be recallable"
    print("OK: jobs resume, credits gate the scheduler, lessons cool sites down + escalate")
