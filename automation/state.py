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

import glob
import json
import os
import sqlite3
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(_DIR), "state.db")

# steps that mean "in flight" — a restart must pick these back up
LIVE = ("submitting", "rendering", "downloading")

# Craft lessons that describe the WORK rather than the FILM. They are real rules and stay enforced
# by the gate/tooling, but they must never be primed into a video-generation prompt — see
# State.film_lessons. Everything not listed here is treated as a film rule.
OFF_FILM = {
    "verify-untracked-before-commit", "use-correct-db-path", "monitor-db-status-not-stdout",
    "record-standing-policy", "defer-unverified-asset-removal", "regression-assert-timing",
}

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
  requeues  INTEGER NOT NULL DEFAULT 0,  -- times the GATE sent this clip back; its own budget
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
  source  TEXT NOT NULL DEFAULT 'manual', -- 'manual' (entered at login) | 'observed' (read off the site)
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
CREATE TABLE IF NOT EXISTS reel_qc (
  project TEXT PRIMARY KEY,       -- project dir name (a reel)
  ok      INTEGER NOT NULL,       -- 1 = gate PASS, 0 = HELD (block/gate items)
  block_n INTEGER NOT NULL,
  gate_n  INTEGER NOT NULL,
  verdict TEXT NOT NULL,          -- json: the actual block/gate/warn/checklist lines
  checked REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS eye_review (
  project TEXT PRIMARY KEY,       -- project dir name (a reel)
  fp      TEXT NOT NULL,          -- the CUT that was looked at (qc.Reel.fp)
  by      TEXT NOT NULL,          -- who looked — the sign-off is a person, not a check
  note    TEXT,                   -- what they saw
  at      REAL NOT NULL
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
    _synced = False               # fingerprints backfilled from disk once per process (see below)

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
                # migration for brains created before credits.source existed
                try:
                    self.db.execute("ALTER TABLE credits ADD COLUMN source TEXT "
                                    "NOT NULL DEFAULT 'manual'")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise            # a lock goes back to the retry loop above
                # …and before jobs.requeues existed (a gate-driven re-shoot needs its own budget:
                # requeue() clears `retries`, so retries cannot count re-shoots)
                try:
                    self.db.execute("ALTER TABLE jobs ADD COLUMN requeues INTEGER NOT NULL "
                                    "DEFAULT 0")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
                self.db.commit()
                return
            except sqlite3.OperationalError as e:
                last = e
                if "locked" not in str(e).lower():
                    raise
                time.sleep(0.04 * (attempt + 1))
        raise last

    # ---- jobs ----
    def add_job(self, project: str, idx: int, prompt: str, first_png: str = None,
                site: str = None) -> int:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO jobs(project,idx,prompt,first_png,site,updated) "
            "VALUES(?,?,?,?,?,?)",
            (project, idx, prompt, first_png, site, time.time()))
        self.db.commit()
        # rowcount, NOT lastrowid: on an IGNOREd insert lastrowid still holds the PREVIOUS
        # successful insert (a dup of project A after project B returned B's id).
        if cur.rowcount:
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

    def requeue(self, project: str, idxs, prompt: str = None) -> list:
        """Put already-done clips back on the queue so they re-render — used when a shipped
        clip is REJECTED (e.g. it came back photoreal while the rest of the reel is 3-D).
        A stale 'done' can never be re-shot by the runner, so re-queueing needs its own seam,
        and it clears last_error/retries so the fresh attempt gets the full retry budget.
        `prompt` rewrites the row's prompt (the hardened style-anchor version), so the queued
        work carries the fix instead of the text that produced the defect.

        `requeues` counts these and is NOT cleared here: retries budget a flaky SITE, requeues
        budget the GATE's patience. Without a counter that survives, "re-shoot the clip the gate
        rejected" is a loop with no exit — the same clip coming back slightly different forever.

        ONE EXCEPTION, and it is the point of the counter: a prompt REPLACED WHOLESALE is a new
        hypothesis, not the same rejection again. The counter exists to stop the SAME feedback
        looping; when the code itself changed what the site is told (2026-10-22: the disaster-shot
        CARTOON lead, after two identical photoreal rejections), spending the old budget on the
        new fix would cap a repair that has not been tried. An APPEND (the 'RE-SHOOT —' note the
        runner folds onto the old prompt) is the same hypothesis and keeps the count."""
        out = []
        for idx in idxs:
            r = self.db.execute("SELECT id, prompt FROM jobs WHERE project=? AND idx=?",
                                (project, idx)).fetchone()
            if not r:
                raise SystemExit(f"no job for {project}#{idx}")
            same_hypothesis = True
            if prompt:
                old = r["prompt"] or ""
                # An APPEND keeps the old prompt and adds to it (the runner's 'RE-SHOOT —' note),
                # so the NEW text starts with the OLD — that is the same hypothesis, and the
                # count goes UP. A REPLACEMENT (the new text does not begin with the old) is a
                # new hypothesis, so the count goes back down.
                same_hypothesis = not old or prompt.startswith(old[:40])
            self.db.execute(
                "UPDATE jobs SET status='queued', retries=0, requeues=MAX(0, requeues+?), "
                "last_error=NULL, updated=?"
                + (", prompt=? " if prompt else " ") + "WHERE id=?",
                ((1 if same_hypothesis else -1, time.time(), prompt, r["id"]) if prompt
                else (1, time.time(), r["id"])))
            out.append(r["id"])
        self.db.commit()
        return out

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

    def project_site(self, project: str) -> str:
        """The ONE source a project is locked to, or None if it has not produced a clip yet.

        Read off the clips that have actually come back, so it records where the reel's look
        really came from rather than any intent. This is the standing rule: a reel is shot on a
        single model, because mixing sites across scenes breaks character consistency — the one
        thing the owner asked for — and no gate can see it (the style pre-flight only proves each
        clip is 3-D, not that the six clips agree with each other)."""
        row = self.db.execute(
            "SELECT site FROM jobs WHERE project=? AND status='done' AND site IS NOT NULL "
            "AND video_path IS NOT NULL ORDER BY idx LIMIT 1", (project,)).fetchone()
        return row["site"] if row else None

    def lock_project(self, project: str, site: str):
        """Move a project that has NOT produced a clip yet onto `site`.

        Whole-reel failover: when the primary source is dry/disabled before the first clip lands,
        the ENTIRE reel follows the runner's pick and this records that decision so every queued
        scene uses the new site. Only meaningful before any clip is done — once one exists,
        project_site() owns the lock and nothing may change it (one reel, one source)."""
        self.db.execute(
            "UPDATE jobs SET site=?, updated=? WHERE project=? AND status='queued'",
            (site, time.time(), project))
        self.db.commit()

    # ---- credits ----
    def set_credits(self, site: str, account: str, remaining: int, resets_at: float = 0,
                    source: str = "manual"):
        """Hand-entered balance (login/signup) — source='manual' so the report can say so
        until a run observes the real number."""
        self.db.execute(
            "INSERT INTO credits(site,account,remaining,resets_at,updated,source) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(site,account) DO UPDATE SET remaining=excluded.remaining, "
            "resets_at=excluded.resets_at, source=excluded.source, updated=excluded.updated",
            (site, account, remaining, resets_at, time.time(), source))
        self.db.commit()

    def observe_credits(self, site: str, account: str, remaining: int, observed_at: float = None):
        """Balance READ OFF THE SITE (source='observed'): refreshes remaining + updated only.
        Keeps the existing resets_at — a page read can't know the refill time — and never
        downgrades the source back to 'manual'. Callers validate the number first."""
        if remaining < 0:
            raise ValueError(f"observed balance must be >= 0, got {remaining}")
        now = observed_at or time.time()
        self.db.execute(
            "INSERT INTO credits(site,account,remaining,resets_at,updated,source) "
            "VALUES(?,?,?,0,?,'observed') "
            "ON CONFLICT(site,account) DO UPDATE SET remaining=excluded.remaining, "
            "updated=excluded.updated, source='observed'",
            (site, account, remaining, now))
        self.db.commit()

    def spend_credit(self, site: str, account: str, n: int = 1):
        self.db.execute(
            "UPDATE credits SET remaining=MAX(0,remaining-?), updated=? WHERE site=? AND account=?",
            (n, time.time(), site, account))
        self.db.commit()

    def mark_dry(self, site: str, account: str, refill_at: float = 0.0):
        """The SITE told us it is empty (quota, paywall, insufficient credits) — record the fact.

        This is not the same as a cooldown and must not be treated as one: the pair is OUT until
        its own refill time, so a ledger that says `remaining=0, resets_at=0` forever is what
        made every dry site look hand-entered and every report call it 'manual'. Source is
        'observed' because the site itself was the source."""
        self.set_credits(site, account, 0, resets_at=refill_at, source="observed")

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

    def credit_line(self, site: str, account: str) -> str:
        """One line saying what the ledger believes about a pair — the only thing the scheduler
        ever sees. `resets_at=0` is printed as "no refill time", because that is the state that
        silently kills a site: a 0 with no refill reads to `available()` as a permanent dead end."""
        r = self.db.execute("SELECT * FROM credits WHERE site=? AND account=?",
                            (site, account)).fetchone()
        if not r:
            return f"{site}/{account}: no row — the site is invisible to the scheduler"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["resets_at"])) \
            if r["resets_at"] else "no refill time"
        age = (time.time() - (r["updated"] or 0)) / 3600.0
        return (f"{site}/{account}: remaining={r['remaining']} resets={when} "
                f"source={r['source']} read {age:.1f}h ago")

    def site_has_credit(self, site: str, now: float = None) -> bool:
        """Does ANY account on this site still have credit left? Deliberately ignores cooldown.

        Cooling is temporary, dry is not, and the scheduler has to tell them apart to choose
        between waiting out a cooldown and failing a whole reel over to a lower-tier site.
        `available()` answers "can I act right now" (both filters) — this answers "is this site
        still worth waiting for" (credit only). `resets_at` is guarded for truthiness, exactly as
        `available()` does: 0 means "no refill time known", NOT "refilled at the epoch"."""
        now = now or time.time()
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM credits WHERE site=? AND "
            "(remaining > 0 OR (resets_at AND resets_at <= ?))",
            (site, now)).fetchone()
        return row["n"] > 0

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

    def film_lessons(self) -> list:
        """The craft lessons that describe the FILM itself — the brief every agent is primed with
        before it writes, and the pre-generation trigger. The tooling lessons below stay out: they
        are instructions about how the work is committed, monitored and verified, not about what
        the reel must contain, and pasting 'verify untracked files before committing' into a
        video-generation prompt is noise the model then has to ignore.

        ponytail: a hand-kept deny-list. Add a slug here when a new process rule appears; a film
        rule wrongly left in costs a few tokens, a process rule wrongly kept costs clarity."""
        return [l for l in self.craft_lessons() if l["slug"] not in OFF_FILM]

    # ---- reel identity fingerprints (so a new reel can't silently reuse an old one's look) ----
    def record_identity(self, reel: str, fp: str):
        self.db.execute(
            "INSERT INTO reel_identity(reel,fp,created) VALUES(?,?,?) "
            "ON CONFLICT(reel) DO UPDATE SET fp=excluded.fp, created=excluded.created",
            (reel, fp, time.time()))
        self.db.commit()

    def other_identities(self, reel: str) -> list:
        """Fingerprints of every OTHER shipped reel — to detect a repeated look."""
        self._sync_identities()
        return self.db.execute(
            "SELECT reel, fp FROM reel_identity WHERE reel!=?", (reel,)).fetchall()

    def _sync_identities(self):
        """Backfill `reel_identity` from projects/*/identity.json, once per process.

        The table is only written when a reel passes the gate, so every reel hand-built or built
        before that seam has NO row: `other_identities` came back empty and both the collision
        check and the art-director's 'differ from these' brief were blind, while 7 shipped looks
        sat on disk. The disk is the durable record (it is committed), so read it back. `projects/`
        sits beside the db in the house layout, so a db without one beside it — a selfcheck's temp
        db — simply has nothing to sync and touches no house state."""
        if State._synced:
            return
        State._synced = True
        try:
            dbp = self.db.execute("PRAGMA database_list").fetchone()[2] or ""
            pdir = os.path.join(os.path.dirname(os.path.abspath(dbp)), "projects") if dbp else ""
            if not pdir or not os.path.isdir(pdir):
                return
            for idp in glob.glob(os.path.join(pdir, "*", "identity.json")):
                name = os.path.basename(os.path.dirname(idp))
                try:
                    fp = json.dumps(json.load(open(idp, encoding="utf-8")), sort_keys=True)
                except Exception:                     # noqa: BLE001 — an unreadable one is skipped
                    continue
                self.db.execute("INSERT OR IGNORE INTO reel_identity(reel,fp,created) "
                                "VALUES(?,?,?)", (name, fp, time.time()))
            self.db.commit()
        except Exception:                             # noqa: BLE001 — a missing record never costs a reel
            pass

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

    # ---- craft-gate verdicts: qc.run() writes the verdict HERE so a HELD reel is durable state,
    # not a printout — brain.report() and `brain.py reels` read it back. ----
    def record_qc(self, project: str, verdict: dict):
        """Persist one craft-gate run: pass/fail + the actual problems, keyed by project."""
        self.db.execute(
            "INSERT INTO reel_qc(project,ok,block_n,gate_n,verdict,checked) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(project) DO UPDATE SET ok=excluded.ok, block_n=excluded.block_n, "
            "gate_n=excluded.gate_n, verdict=excluded.verdict, checked=excluded.checked",
            (project, 1 if verdict.get("ok") else 0, len(verdict.get("block", [])),
             len(verdict.get("gate", [])),
             json.dumps({k: verdict.get(k) for k in ("ok", "live", "block", "gate", "warn")}
                        | {"checklist": len(verdict.get("checklist", []))},
                        ensure_ascii=False),
             time.time()))
        self.db.commit()

    def qc_verdicts(self, project: str = None) -> list:
        """Saved gate verdicts (newest first); one project when asked."""
        if project:
            row = self.db.execute("SELECT * FROM reel_qc WHERE project=?", (project,)).fetchone()
            return [row] if row else []
        return self.db.execute("SELECT * FROM reel_qc ORDER BY checked DESC").fetchall()

    # ---- the by-eye sign-off. When a review could NOT run (down gateway) the reel may ship but
    # does NOT go_live until a human has looked (WAVELENGTH §6). Keyed on the CUT: a rebuild is a
    # different cut that nobody has seen, so the old signature stops counting (qc.run's `live`). ----
    def record_eye_review(self, project: str, fp: str, by: str, note: str = None):
        """Record a human's verdict on one cut of one reel. Upsert: the newest look is the one."""
        self.db.execute(
            "INSERT INTO eye_review(project,fp,by,note,at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(project) DO UPDATE SET fp=excluded.fp, by=excluded.by, "
            "note=excluded.note, at=excluded.at",
            (project, fp, by, note, time.time()))
        self.db.commit()

    def eye_review(self, project: str):
        """The sign-off on this reel, or None. Compare its `fp` to the cut before trusting it."""
        return self.db.execute("SELECT * FROM eye_review WHERE project=?", (project,)).fetchone()

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
    other = st.add_job("other", 0, "different project")     # a later successful insert
    assert st.add_job("proj", 0, "dup") == j, \
        "a deduped add must return ITS OWN id, not the last insert's (stale lastrowid)"
    assert other != j, "separate projects get separate ids"
    st.set_status(other, "done", video_path="/x/other.mp4")
    assert st.db.execute("SELECT project FROM jobs WHERE id=?", (j,)).fetchone()["project"] == "proj", \
        "the returned id must address the job the caller asked for"
    st.set_status(j, "rendering", site="kling", account="a1")
    assert [r["id"] for r in st.resume()] == [j], "rendering row must be resumable"
    st.set_status(j, "done", video_path="/x/0.mp4")
    assert not st.resume(), "done row must not resume"

    # requeue: a REJECTED clip that already says 'done' must be re-shootable, and the retry
    # budget must be fresh (otherwise a re-render inherits the old clip's burned retries)
    st.set_status(j, "error", video_path=None)
    st.bump_retry(j)
    st.bump_retry(j)
    assert st.requeue("proj", [0], "hardened style-anchored prompt") == [j], "requeue returns ids"
    row = st.db.execute("SELECT status,retries,last_error,prompt FROM jobs WHERE id=?", (j,)).fetchone()
    assert row["status"] == "queued" and row["retries"] == 0 and not row["last_error"], dict(row)
    assert row["prompt"] == "hardened style-anchored prompt", "a requeued job carries the FIX"
    assert st.next_queued()["id"] == j, "the requeued clip is next out of the queue"
    st.set_status(j, "done", video_path="/x/0.mp4")      # put it back for the rest of the check
    try:
        st.requeue("proj", [7])
        raise AssertionError("requeueing a clip with no job row must fail loudly")
    except SystemExit:
        pass

    # credits + scheduler availability
    st.set_credits("kling", "a1", remaining=6)
    st.set_credits("veo", "a1", remaining=0, resets_at=time.time() + 9999)  # dry, not due
    avail = {(r["site"], r["account"]) for r in st.available()}
    assert ("kling", "a1") in avail and ("veo", "a1") not in avail, avail
    st.spend_credit("kling", "a1", 6)
    assert not st.available(), "spent-out + not-due -> nothing available"

    # credits say WHERE the number came from: hand-entered at login vs observed off the site
    st.set_credits("krea", "a1", remaining=30, resets_at=time.time() + 5000)
    src = st.db.execute("SELECT source FROM credits WHERE site='krea'").fetchone()["source"]
    assert src == "manual", "a login-time balance is labelled manual"
    st.observe_credits("krea", "a1", 42)
    row = st.db.execute("SELECT remaining, resets_at, source FROM credits WHERE site='krea'").fetchone()
    assert row["remaining"] == 42 and row["source"] == "observed", dict(row)
    assert row["resets_at"] > time.time(), \
        "an observed read refreshes remaining only — it must not wipe the known refill time"
    try:
        st.observe_credits("krea", "a1", -1); assert False, "negative balance must be rejected"
    except ValueError:
        pass

    # A SITE SAYING IT IS OUT IS A FACT, NOT A COOLDOWN (owner fix, 2026-10-14). Marked dry with
    # the refill time it named: the scheduler stops offering it, and the moment that time passes
    # it is offered again without anyone re-typing a number.
    st.mark_dry("luma", "a1", refill_at=time.time() + 3600)
    row = st.db.execute("SELECT remaining, resets_at, source FROM credits WHERE site='luma'").fetchone()
    assert row["remaining"] == 0 and row["source"] == "observed", dict(row)
    assert row["resets_at"] > time.time(), "the refill time the site named is held"
    assert ("luma", "a1") not in {(r["site"], r["account"]) for r in st.available()}, \
        "an empty site must not be offered to the scheduler"
    assert not st.site_has_credit("luma"), "and it is not worth waiting for either"
    st.mark_dry("hailuo", "a1")                      # a site that named no period at all
    assert st.db.execute("SELECT resets_at FROM credits WHERE site='hailuo'").fetchone()[0] == 0, \
        "no named refill stays unknown — 0 is 'not known', never a guessed date"
    st.mark_dry("luma", "a1", refill_at=time.time() - 1)   # the refill time comes round
    assert ("luma", "a1") in {(r["site"], r["account"]) for r in st.available()}, \
        "once the refill time passes the site is offered again, with no re-typing"

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

    # film_lessons: the pre-generation brief. A process rule (commit hygiene) must be held back
    # from the agents — pasting it into a video prompt is noise — while a film rule reaches them.
    st.learn_lesson("verify-untracked-before-commit", "Verify untracked files before committing")
    _film = {l["slug"] for l in st.film_lessons()}
    assert "caption-safe-area" in _film, "a craft rule about the film must reach the agents"
    assert "verify-untracked-before-commit" not in _film, \
        "a process rule must never be primed into a video-generation prompt"

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

    # craft-gate verdicts: persisted so a HELD reel is durable, and re-running overwrites
    st.record_qc("reel_x", {"ok": False, "block": ["no audio"], "gate": [], "warn": [], "checklist": []})
    st.record_qc("reel_x", {"ok": True, "block": [], "gate": [], "warn": [], "checklist": []})
    q = st.qc_verdicts("reel_x")[0]
    assert q["ok"] == 1 and q["block_n"] == 0, "a re-run overwrites the stored verdict"
    st.record_qc("reel_y", {"ok": False, "block": ["frozen still"], "gate": [], "warn": [], "checklist": []})
    assert {r["project"] for r in st.qc_verdicts()} == {"reel_x", "reel_y"}
    assert st.qc_verdicts("nope") == [], "unknown project -> no verdict"

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
    print("OK: deduped add_job returns its own id; craft-gate verdicts persist and re-run overwrites")
    print("OK: a rejected 'done' clip requeues with a fresh retry budget and the fixed prompt")
    print("OK: credits are labelled manual vs observed; an observed read keeps resets_at")
