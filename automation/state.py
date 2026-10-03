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
CREATE TABLE IF NOT EXISTS craft_lessons (
  id INTEGER PRIMARY KEY,
  slug     TEXT NOT NULL UNIQUE,          -- stable id, e.g. 'caption-safe-area'
  title    TEXT NOT NULL,
  why      TEXT,                          -- why it matters (for the judgment checklist)
  tier     TEXT NOT NULL DEFAULT 'auto',  -- 'auto' (code can check) | 'judgment' (needs eyes)
  check_id TEXT,                          -- key into qc.CHECKS for auto lessons; NULL for judgment
  severity TEXT NOT NULL DEFAULT 'block', -- 'block' | 'publish-gate' | 'warn'
  source   TEXT,                          -- where it came from (owner correction + date)
  created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reel_identity (
  reel    TEXT PRIMARY KEY,               -- project name of a shipped reel
  fp      TEXT NOT NULL,                  -- json fingerprint of its distinctive craft choices
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS brain_log (
  id INTEGER PRIMARY KEY,
  ts      REAL NOT NULL,
  session TEXT,                           -- groups events from one work session
  kind    TEXT NOT NULL,                  -- 'edit'|'write'|'bash'|'session'|'note'|...
  detail  TEXT,                           -- concise what-happened
  processed INTEGER NOT NULL DEFAULT 0     -- 1 once reflect() has distilled it
);
CREATE TABLE IF NOT EXISTS candidate_lessons (
  id INTEGER PRIMARY KEY,
  slug     TEXT NOT NULL UNIQUE,
  title    TEXT NOT NULL,
  why      TEXT,
  evidence TEXT,                          -- the pattern/count that triggered it
  hits     INTEGER NOT NULL DEFAULT 1,    -- times the miner has seen this pattern
  status   TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | dismissed
  created  REAL NOT NULL,
  updated  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_lessons_site ON lessons(site, account);
CREATE INDEX IF NOT EXISTS ix_brainlog_session ON brain_log(session, processed);
"""


class State:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        # PRAGMA journal_mode does NOT honour sqlite's busy timeout, and the brain's hooks are
        # legitimately concurrent (several tools finish at once -> several brain.py processes).
        # A fresh writer that meets another one mid-commit gets an immediate 'database is locked'
        # — and a lost insert would silently blind the sense stream. Back off and retry; the
        # lock only lives for one commit, so this resolves in milliseconds.
        last = None
        for attempt in range(25):
            try:
                self.db.execute("PRAGMA journal_mode=WAL")     # survive a mid-write crash
                self.db.execute("PRAGMA synchronous=NORMAL")
                self.db.executescript(_SCHEMA)
                self.db.commit()
                return
            except sqlite3.OperationalError as e:
                last = e
                if "locked" not in str(e).lower():
                    raise
                time.sleep(0.04 * (attempt + 1))
        raise last

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

    # ---- craft lessons (reel quality) — the project's own "brain" of what the owner taught.
    # Separate from the anti-ban `lessons` table on purpose: these are keyed by a stable slug and
    # carry a check_id/severity, not a (site,account) cooldown. qc.py runs the checks over them.
    def learn_lesson(self, slug: str, title: str, why: str = None, tier: str = "auto",
                     check_id: str = None, severity: str = "block", source: str = None):
        """Absorb a lesson (owner correction or confirmed good practice). Upsert by slug so
        re-teaching the same thing refines the row instead of duplicating it."""
        self.db.execute(
            "INSERT INTO craft_lessons(slug,title,why,tier,check_id,severity,source,created) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, why=excluded.why, "
            "tier=excluded.tier, check_id=excluded.check_id, severity=excluded.severity, "
            "source=excluded.source",
            (slug, title, why, tier, check_id, severity, source, time.time()))
        self.db.commit()

    def craft_lessons(self, tier: str = None) -> list:
        if tier:
            return self.db.execute(
                "SELECT * FROM craft_lessons WHERE tier=? ORDER BY id", (tier,)).fetchall()
        return self.db.execute("SELECT * FROM craft_lessons ORDER BY id").fetchall()

    # ---- reel identity fingerprints (so a new reel can't silently reuse an old one's look) ----
    def record_identity(self, reel: str, fp: str):
        self.db.execute(
            "INSERT INTO reel_identity(reel,fp,created) VALUES(?,?,?) "
            "ON CONFLICT(reel) DO UPDATE SET fp=excluded.fp, created=excluded.created",
            (reel, fp, time.time()))
        self.db.commit()

    def other_identities(self, reel: str) -> list:
        """Fingerprints of every OTHER shipped reel — to detect a repeated look."""
        return self.db.execute(
            "SELECT reel, fp FROM reel_identity WHERE reel!=?", (reel,)).fetchall()

    # ---- brain activity log: the always-on sense stream (hooks append here as the session works;
    # reflect() distills the unprocessed rows into durable knowledge at session end). ----
    def log_event(self, kind: str, detail: str = None, session: str = None):
        self.db.execute(
            "INSERT INTO brain_log(ts,session,kind,detail,processed) VALUES(?,?,?,?,0)",
            (time.time(), session, kind, detail))
        self.db.commit()

    def events(self, session: str = None, processed: bool = None, limit: int = 1000) -> list:
        sql, args = "SELECT * FROM brain_log WHERE 1=1", []
        if session is not None:
            sql += " AND session=?"; args.append(session)
        if processed is not None:
            sql += " AND processed=?"; args.append(1 if processed else 0)
        sql += " ORDER BY ts LIMIT ?"; args.append(limit)
        return self.db.execute(sql, args).fetchall()

    def mark_processed(self, session: str):
        """Mark a session's raw events distilled — keep the 'session' summary rows unprocessed."""
        self.db.execute(
            "UPDATE brain_log SET processed=1 WHERE session=? AND kind!='session'", (session,))
        self.db.commit()

    # ---- candidate lessons: the brain PROPOSES rules from patterns; the owner vets them.
    # These never reach craft_lessons (the QC gate) until promote() copies an accepted one over. ----
    def propose_lesson(self, slug: str, title: str, why: str = None, evidence: str = None):
        """File/refresh a candidate. Upsert by slug; a candidate the owner already accepted or
        dismissed is left frozen (the WHERE guard), so the miner can't re-nag about it."""
        now = time.time()
        self.db.execute(
            "INSERT INTO candidate_lessons(slug,title,why,evidence,hits,status,created,updated) "
            "VALUES(?,?,?,?,1,'pending',?,?) "
            "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, why=excluded.why, "
            "evidence=excluded.evidence, hits=candidate_lessons.hits+1, updated=excluded.updated "
            "WHERE candidate_lessons.status='pending'",
            (slug, title, why, evidence, now, now))
        self.db.commit()

    def candidates(self, status: str = "pending") -> list:
        if status:
            return self.db.execute(
                "SELECT * FROM candidate_lessons WHERE status=? ORDER BY hits DESC, updated DESC",
                (status,)).fetchall()
        return self.db.execute("SELECT * FROM candidate_lessons ORDER BY updated DESC").fetchall()

    def resolve_candidate(self, slug: str, status: str):
        self.db.execute("UPDATE candidate_lessons SET status=?, updated=? WHERE slug=?",
                        (status, time.time(), slug))
        self.db.commit()
        return self.db.execute("SELECT * FROM candidate_lessons WHERE slug=?", (slug,)).fetchone()



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

    # craft lessons: absorb, refine (upsert by slug), query by tier
    st.learn_lesson("caption-safe-area", "Captions out of the bottom/right UI zone",
                    why="the app paints handle/CTA there", check_id="caption-safe-area")
    st.learn_lesson("style-consistency", "No photoreal<->animated drift", tier="judgment")
    st.learn_lesson("caption-safe-area", "Captions in the top-safe strip",   # re-teach = refine
                    why="platform UI covers the bottom ~18%", check_id="caption-safe-area")
    all_l = st.craft_lessons()
    assert len(all_l) == 2, f"upsert by slug must not duplicate: {len(all_l)}"
    assert [l["slug"] for l in all_l if l["slug"] == "caption-safe-area"][0]
    assert st.craft_lessons(tier="judgment")[0]["slug"] == "style-consistency"

    # identity fingerprints: only OTHER reels count as a collision surface
    st.record_identity("reel_a", '{"opener":"reveal","font":"Didot"}')
    st.record_identity("reel_b", '{"opener":"quote","font":"Snell"}')
    assert {r["reel"] for r in st.other_identities("reel_a")} == {"reel_b"}, "exclude self"

    # brain log: events accrue unprocessed, reflect marks them, session summaries persist
    st.log_event("edit", "touched build.py", session="s1")
    st.log_event("bash", "ran assemble", session="s1")
    assert len(st.events(session="s1", processed=False)) == 2, "raw events are unprocessed"
    st.log_event("session", "s1 summary", session="s1")       # a distilled summary row
    st.mark_processed("s1")
    assert not st.events(session="s1", processed=False, limit=10) or \
        all(e["kind"] == "session" for e in st.events(session="s1", processed=False)), \
        "mark_processed clears raw rows but keeps the session summary"

    # candidate lessons: propose (upsert bumps hits), owner dismiss freezes it against re-nag
    st.propose_lesson("wall-kling-captcha", "kling keeps hitting captcha", evidence="count=3")
    st.propose_lesson("wall-kling-captcha", "kling keeps hitting captcha", evidence="count=4")
    assert st.candidates()[0]["hits"] == 2, "re-proposing the same slug bumps hits, not duplicates"
    st.resolve_candidate("wall-kling-captcha", "dismissed")
    st.propose_lesson("wall-kling-captcha", "x", evidence="count=5")   # must NOT revive it
    assert not st.candidates(status="pending"), "a dismissed candidate stays dismissed (no re-nag)"
    assert st.candidates(status="dismissed")[0]["hits"] == 2, "frozen hits unchanged after dismiss"
    print("OK: jobs resume, credits gate the scheduler, lessons cool sites down + escalate")
    print("OK: craft lessons upsert-by-slug + tier query, identity fingerprints recorded")
    print("OK: brain log senses events + reflect marks them processed")
    print("OK: candidate lessons upsert + freeze on owner decision (vetting gate holds)")
