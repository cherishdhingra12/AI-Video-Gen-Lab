"""
brain.py — the LOCAL BRAIN of AI Video Gen Lab.

One project, one brain. It owns everything this project can decide and do on its
own — the way a body runs on reflexes without asking permission: perceive,
remember, learn, judge, act. Later, every project's local brain plugs into one
central "mother brain"; this file is the single front door that makes that plug
trivial (see report() / the mother-brain seam at the bottom).

It does NOT re-implement the organs — it is the cortex that ties them together:

    perception  automation/vision.py   screenshot -> state / captcha (gemini)
    memory      automation/state.py    durable SQLite: jobs, credits, lessons, craft, identity
    reflex      automation/learn.py    route around walls, escalating cooldown (the Brain that picks)
    judgment    automation/qc.py       the craft gate — ship? go_live?
    motor       automation/runner.py   the loop: drive one clip, resume after a crash
    planning    automation/agents.py   idea -> script -> per-clip prompts

    python3 brain.py                   # this brain's self-report (health, budget, work, knowledge)
    python3 brain.py review <project>  # run the craft gate on a reel (verdict saved to state.db)
    python3 brain.py reels             # every finished reel + its gate verdict (pass/held/unchecked)
    python3 brain.py --selfcheck       # offline: prove the report aggregates correctly

Mother-brain seam (defined, NOT wired — YAGNI until the central brain exists):
  report()        -> a plain JSON-able dict = the READ port a central brain polls.
  directive(cmd)  -> the COMMAND port; stubbed to read-only ops. Wire a transport
                     here when there's something on the other end. Contract: BRAIN.md.
"""
import glob
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
AUT = os.path.join(ROOT, "automation")
sys.path.insert(0, AUT)
from state import State      # noqa: E402
import qc                    # noqa: E402

PROJECTS = os.path.join(ROOT, "projects")


def _final(project_dir: str):
    """Newest finished cut in a project dir — `final.mp4` from the runner/stitch path or
    `final_veo.mp4` from the craft path — or None. Same rule qc.Reel uses, so the brain and
    the gate never disagree about what 'the reel' is (a runner-built reel used to be invisible)."""
    found = [os.path.join(project_dir, f) for f in qc.Reel.FINALS
             if os.path.exists(os.path.join(project_dir, f))]
    return max(found, key=os.path.getmtime) if found else None

# pattern-mining thresholds: how many times a failure must recur before the brain proposes a rule
WALL_MIN = 3     # same (site, wall-kind) this many times -> candidate lesson
FAIL_MIN = 2     # this many failed jobs on one site -> candidate lesson


class LocalBrain:
    """The project's single front door. A thin cortex over the organs in automation/ —
    callers (a human, a cron, and later the mother brain) talk to the brain, not the parts."""
    name = "ai-video-gen-lab"

    def __init__(self, st: State = None):
        self.st = st or State()

    def close(self):
        self.st.close()

    # ---- memory / learning passthroughs (never reach past the brain) ----
    def learn(self, **kw):
        """Teach a craft lesson (owner correction or confirmed practice). See state.learn_lesson."""
        self.st.learn_lesson(**kw)

    def review(self, project: str):
        """Judgment: run the craft gate on a reel dir (delegates to qc.run; the verdict is
        saved to state.db so HELD stays visible in report() / `reels`)."""
        d = project if os.path.isdir(project) else os.path.join(PROJECTS, project)
        if not os.path.isdir(d):
            raise SystemExit(f"no such project: {d}")
        return qc.run(d, st=self.st)

    # ---- the self-report: read port for a human AND the future mother brain ----
    def report(self) -> dict:
        db, now = self.st.db, time.time()
        work = {r["status"]: r["n"]
                for r in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        budget, depleted, cooling = [], [], []
        for c in db.execute("SELECT site,account,remaining,resets_at,updated,source FROM credits"):
            cd = self.st.cooldown_left(c["site"], c["account"], now)
            it = {"site": c["site"], "account": c["account"], "remaining": c["remaining"],
                  "source": c["source"] or "manual"}
            if it["source"] == "observed":
                it["read_ago_min"] = round((now - c["updated"]) / 60)   # age of the live read
            if cd > 0:
                cooling.append({**it, "cooldown_min": round(cd / 60, 1)})
            elif c["remaining"] > 0 or (c["resets_at"] and c["resets_at"] <= now):
                budget.append(it)
            else:
                depleted.append(it)
        cl = self.st.craft_lessons()
        gating = sum(1 for l in cl if l["severity"] in ("block", "publish-gate"))
        reels = db.execute("SELECT COUNT(*) n FROM reel_identity").fetchone()["n"]
        pending = len(self.st.candidates(status="pending"))
        # finished reels = either final name, each with its persisted craft-gate verdict
        qcs = {q["project"]: q for q in self.st.qc_verdicts()}
        out = []
        if os.path.isdir(PROJECTS):
            for p in sorted(os.listdir(PROJECTS)):
                fin = _final(os.path.join(PROJECTS, p))
                if not fin:
                    continue
                q = qcs.get(p)
                out.append({"project": p, "final": os.path.relpath(fin, ROOT),
                            "qc": ("pass" if q and q["ok"] else "held" if q else "unchecked"),
                            "block": q["block_n"] if q else 0,
                            "gate": q["gate_n"] if q else 0})
        held = [x["project"] for x in out if x["qc"] == "held"]
        needs = []
        if work.get("failed"):
            needs.append(f"{work['failed']} job(s) failed — inspect last_error")
        if (work.get("queued") or self.st.resume()) and not budget:
            needs.append("work pending but every source is depleted or cooling — wait for reset / re-login")
        # A reel is locked to ONE source (one look), so if that source is dry or cooling the reel
        # WAITS — correctly. But when other sites still have credit the check above stays quiet and
        # the reel stalls with no way to notice. Name the reel and the source it is waiting on.
        stuck = []
        for r in db.execute("SELECT DISTINCT project FROM jobs "
                            "WHERE status IN ('queued','submitting','rendering','downloading')"):
            site = self.st.project_site(r["project"])
            if not site:
                continue                       # not shot yet — free to start anywhere
            ok = any(c["remaining"] > 0 and self.st.cooldown_left(site, c["account"], now) <= 0
                     for c in db.execute("SELECT account,remaining FROM credits WHERE site=?", (site,)))
            if not ok:
                stuck.append(f"{r['project']} (locked to {site})")
        if stuck:
            needs.append("reel(s) waiting on their one source — top it up or re-login: "
                         + ", ".join(stuck))
        if held:
            needs.append(f"{len(held)} finished reel(s) HELD by the craft gate — "
                         f"brain.py review <project>: {', '.join(held)}")
        if pending:
            needs.append(f"{pending} proposed lesson(s) await review — brain.py candidates")
        return {
            "brain": self.name, "root": ROOT, "ts": now,
            "healthy": not needs,
            "work": work, "resume_backlog": len(self.st.resume()),
            "budget_now": budget, "depleted": depleted, "cooling": cooling,
            "knowledge": {"craft_lessons": len(cl), "gating": gating,
                          "reels_fingerprinted": reels, "candidates_pending": pending},
            "deliverables": [x["project"] for x in out], "reels": out, "needs": needs,
        }

    # ---- mother-brain COMMAND port (defined, not wired) ----
    def directive(self, cmd: dict):
        """Future: the central brain sends {"op":..., ...}. Only read-only ops are exposed until
        a transport + auth exist. ponytail: wire a real transport (and widen ops) when the mother
        brain is live; keep the surface tiny and auditable until then — contract in BRAIN.md."""
        op = (cmd or {}).get("op") or "report"
        if op in ("report", "status"):
            return self.report()
        if op == "review":
            return self.review(cmd["project"])
        raise NotImplementedError(f"directive '{op}' is not available until the central brain is wired")

    # ---- always-on sensing: hooks feed session activity in; reflect() distills it ----
    @staticmethod
    def _short(p):
        try:
            return os.path.relpath(p, ROOT) if p and str(p).startswith(ROOT) else p
        except Exception:
            return p

    def observe(self, hook: dict, failed: bool = False):
        """Record ONE session event (called by a PostToolUse hook on every change, and by a
        PostToolUseFailure hook when a tool errors). Append-only, cheap — this is the brain
        sensing the project work, successes AND failures, even when nobody tells it."""
        tool = (hook or {}).get("tool_name", "?")
        ti = (hook or {}).get("tool_input", {}) or {}
        sess = (hook or {}).get("session_id")
        if tool in ("Edit", "Write", "NotebookEdit"):
            kind = tool.lower()
            detail = self._short(ti.get("file_path") or ti.get("notebook_path"))
        elif tool == "Bash":
            kind, detail = "bash", (ti.get("command", "") or "")[:200]
        else:
            kind, detail = tool.lower(), None
        if failed:
            kind += "_fail"          # a sensed failure — the miner groups these into patterns
        self.st.log_event(kind, detail, session=sess)

    def reflect(self, hook: dict) -> dict:
        """Distill a session's raw events into ONE durable session note (called at session end).
        Summarizes WHAT happened; promoting a new craft lesson stays owner-vetted — reflect only
        SURFACES candidates in needs_review, it does not auto-write craft_lessons."""
        sess = (hook or {}).get("session_id")
        raw = [e for e in self.st.events(session=sess, processed=False) if e["kind"] != "session"]
        if not raw:
            return {"session": sess, "events": 0}
        by_kind, files, cmds = {}, set(), []
        for e in raw:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
            if e["kind"] in ("edit", "write", "notebookedit") and e["detail"]:
                files.add(e["detail"])
            elif e["kind"] == "bash" and e["detail"]:
                cmds.append(e["detail"])
        projects = sorted({f.split("/")[1] for f in files
                           if f.startswith("projects/") and len(f.split("/")) > 2})
        needs = [f"re-review reel '{p}' — changed this session" for p in projects]
        self.propose()                       # mine durable patterns -> candidate lessons (owner-vetted)
        pending = self.st.candidates(status="pending")
        if pending:
            needs.append(f"{len(pending)} proposed lesson(s) await review — brain.py candidates")
        note = {"when": time.strftime("%Y-%m-%d %H:%M"), "events": len(raw), "by_kind": by_kind,
                "files": sorted(files)[:40], "projects": projects,
                "commands": cmds[-20:], "needs_review": needs,
                "candidates_pending": [c["slug"] for c in pending]}
        self.st.log_event("session", json.dumps(note, ensure_ascii=False), session=sess)
        if sess:
            self.st.mark_processed(sess)
        return {"session": sess, **note}

    def sessions(self, limit: int = 10) -> list:
        """The brain's episodic memory: the most recent distilled session notes."""
        rows = [e for e in self.st.events(limit=5000) if e["kind"] == "session"]
        return rows[-limit:]

    # ---- the continuous self-learning loop: every finished output TEACHES the brain --------
    #
    # The owner taught "captions / VO / moral / score / one style" several times and the brain
    # still shipped reels without them, because knowing a rule and ENFORCING it were different
    # systems. This closes that loop: after every generation the brain re-audits the artifact it
    # just produced, writes what it actually did into durable state, and files a candidate when
    # the reel is missing a layer it has been told to ship. Continuous, not a one-time fix.

    def _layers(self, project_dir):
        """What craft layers this reel actually SHIPS, measured from the artifacts on disk —
        not from what the project claims. A card that exists but sits in the IG UI zone counts
        as present here (the safe-area gate owns that) — this asks 'did we even build it?'."""
        d = os.path.abspath(project_dir)
        name = os.path.basename(d.rstrip("/"))
        # Narration is NOT a folder any more. The house rule (WAVELENGTH.md §3) is that speech is
        # spoken inside the generated scenes, so the evidence is the source clips' own audio — a
        # `vo/` dir is not evidence of anything. This is the same measurement qc.craft-layers makes.
        clips = sorted(glob.glob(os.path.join(qc._CLIPS_ROOT, name, "clip_*.mp4")))
        return {
            "captions": sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "caption_*.png"))),
            "narration": [os.path.basename(c) for c in clips if qc._has_audio(c)],
            "clips": len(clips),
            "score": bool(glob.glob(os.path.join(d, "music_raw.mp3"))),
            "moral": bool(glob.glob(os.path.join(d, "*moral*.png"))),
            "identity": bool(glob.glob(os.path.join(d, "identity.json"))),
        }

    _LAYER_RULES = {
        "identity": ("reel-needs-identity",
                     "Every reel carries its own art direction (identity.json) — opener, type, "
                     "quote, structure — and it must not repeat a shipped reel's look",
                     "a reel with no identity drifts back to the house default look, and two "
                     "reels that share a fingerprint are the same film twice"),
        "narration": ("reel-needs-narration",
                      "The recurring character SPEAKS inside the generated scenes — no narration "
                      "laid over the top at assembly",
                      "the owner's rule: the clip's own voice is the narration, so lip and line "
                      "agree; a track added afterwards is the thing that was banned"),
        "moral": ("reel-needs-moral",
                  "Every reel lands a moral card — the takeaway is what earns shares and comments",
                  "a story without a stated moral gets scrolled past; the moral IS the viral beat"),
        }
    # NB: there is deliberately NO "score" rule here. A reel's score may ride INSIDE the
    # generated clips — Google Vids bakes its own bed — and the owner rejected both tracks laid
    # over dadi-ki-bucket-list (2026-10-18), so "no music_raw.mp3" is a choice, not a gap. What
    # is a defect — a reel that plays flat — qc.check_score_present measures off the shipped cut.
    # The old rule filed a candidate on every legitimately bed-less reel; it is dismissed in the
    # brain (2026-10-19) and must not come back.

    def absorb(self, project: str) -> dict:
        """Called after EVERY generation (a stitched final, a craft-path build). Audits the
        artifact, records what actually shipped, and files a candidate for any layer the brain
        has been told to include but that is missing. Fail-open: never breaks a build."""
        d = project if os.path.isdir(project) else os.path.join(PROJECTS, project)
        if not os.path.isdir(d):
            return {"ok": False, "reason": "no such project"}
        name = os.path.basename(d.rstrip("/"))
        try:
            lay = self._layers(d)
            reel = qc.Reel(d)
            final = reel.final
            # the facts of this generation, durable — so the brain remembers what it shipped
            fact = {"when": time.strftime("%Y-%m-%d %H:%M"), "reel": name, "layers": lay,
                    "final": os.path.basename(final) if final else None,
                    "qc": (self.st.qc_verdicts(name)[0]["ok"] if self.st.qc_verdicts(name) else None)}
            self.st.log_event("generation", json.dumps(fact, ensure_ascii=False))
            filed = []
            for key, (slug, title, why) in self._LAYER_RULES.items():
                if key == "narration":
                    # clips swept after publish means there is nothing to inspect. qc's
                    # craft-layers makes the same call, and without it every old reel would
                    # become a permanent gap the day its clips were cleaned up.
                    present = bool(lay["narration"]) or not lay["clips"]
                else:
                    present = lay[key] if isinstance(lay[key], list) else bool(lay[key])
                if present:
                    continue
                # missing layer the owner has already asked for -> file it as a candidate with
                # this reel as the evidence, so the pattern is visible and promotable
                ev = f"reel '{name}' shipped with no {key} (generation {fact['when']})"
                self.st.propose_lesson(slug, title=title, why=why, evidence=ev)
                filed.append(slug)
            if not lay["captions"]:
                ev = f"reel '{name}' shipped with no caption cards"
                self.st.propose_lesson("reel-needs-captions",
                                       title="Every reel ships on-screen captions/headings",
                                       why="a reel with no on-screen text is unreadable muted "
                                           "and unreachable for the algorithm",
                                       evidence=ev)
                filed.append("reel-needs-captions")
            out = {"ok": True, "reel": name, "learned": filed}
            if filed:
                out["note"] = f"{len(filed)} layer gap(s) filed as candidates"
            return out
        except Exception as e:                        # fail-open: a build never dies on bookkeeping
            return {"ok": False, "error": str(e)[:120]}

    # ---- pattern mining: PROPOSE craft lessons from recurring failures. Owner-vetted: a
    # candidate never enters the QC gate until promote() copies an accepted one into craft_lessons. ----
    _WALL_HINT = {"captcha": "needs manual login / deprioritize this source",
                  "rate_limit": "lower cadence or spread across accounts",
                  "selector": "adapter selectors drifted — update the site adapter",
                  "block": "source is blocking automation — back off / rotate account"}

    def propose(self) -> list:
        """Mine durable memory (anti-ban walls + job failures) for recurring patterns and file
        candidate lessons. A repeated wall is a pattern, not noise — but the owner decides."""
        db, filed = self.st.db, []
        for r in db.execute(
                "SELECT site, kind, COUNT(*) n FROM lessons WHERE kind IS NOT NULL "
                "GROUP BY site, kind HAVING n >= ?", (WALL_MIN,)):
            slug = f"wall-{r['site']}-{r['kind']}"
            self.st.propose_lesson(
                slug, title=f"{r['site']}: recurring {r['kind']} ({r['n']}×) — "
                            f"{self._WALL_HINT.get(r['kind'], 'investigate')}",
                why=f"{r['kind']} hit {r['n']}× on {r['site']}; a repeated wall is a pattern worth a rule.",
                evidence=f"lessons: site={r['site']} kind={r['kind']} count={r['n']}")
            filed.append(slug)
        for r in db.execute(
                "SELECT site, COUNT(*) n FROM jobs WHERE status='failed' AND site IS NOT NULL "
                "GROUP BY site HAVING n >= ?", (FAIL_MIN,)):
            slug = f"failrate-{r['site']}"
            self.st.propose_lesson(
                slug, title=f"{r['site']}: {r['n']} failed jobs — reliability pattern",
                why=f"{r['n']} jobs failed on {r['site']}; recurring failure suggests a systemic issue.",
                evidence=f"jobs: site={r['site']} failed={r['n']}")
            filed.append(slug)
        # 3) in-session repeated failures: the same action (sensed via PostToolUseFailure) that
        #    keeps erroring is a pattern — maybe a broken step, maybe a rule the owner wants.
        for r in db.execute(
                "SELECT detail, COUNT(*) n FROM brain_log WHERE kind LIKE '%_fail' "
                "AND detail IS NOT NULL AND detail != '' GROUP BY detail HAVING n >= ?", (FAIL_MIN,)):
            key = r["detail"][:70]
            slug = "cmdfail-" + (re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40] or "x")
            self.st.propose_lesson(
                slug, title=f"recurring failure ({r['n']}×): {key}",
                why=f"this action errored {r['n']}×; a repeated failure is a pattern — fix the step or capture a rule.",
                evidence=f"brain_log fail: detail='{key}' count={r['n']}")
            filed.append(slug)
        return filed

    def promote(self, slug: str, severity: str = "warn"):
        """Owner accepts a candidate -> it becomes a real craft lesson (enters the QC gate)."""
        c = next((x for x in self.st.candidates(status=None) if x["slug"] == slug), None)
        if not c:
            raise SystemExit(f"no candidate '{slug}' (see: brain.py candidates)")
        self.learn(slug=slug, title=c["title"], why=c["why"], tier="judgment", severity=severity,
                   source=f"brain-proposed {time.strftime('%Y-%m-%d')} ({c['evidence']})")
        self.st.resolve_candidate(slug, "accepted")
        return c

    def dismiss(self, slug: str):
        """Owner rejects a candidate -> frozen, the miner won't re-nag about it."""
        return self.st.resolve_candidate(slug, "dismissed")

    # ---- autonomous conversation learning: read the live transcript, extract durable CRAFT
    # lessons via the LLM, and write them straight to the brain (no approval). Advisory tier
    # (judgment/warn) so self-learning can inform every reel's checklist but never auto-BLOCK one. ----
    _CURSOR_FILE = os.path.join(AUT, ".harvest_cursors.json")

    @staticmethod
    def _turn_text(o):
        if not isinstance(o, dict) or o.get("type") not in ("user", "assistant"):
            return None, ""
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        c = msg.get("content")
        if isinstance(c, list):
            txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        else:
            txt = c if isinstance(c, str) else ""
        return o.get("type"), txt.strip()

    def _cursor(self, sess, new=None):
        try:
            cur = json.load(open(self._CURSOR_FILE)) if os.path.exists(self._CURSOR_FILE) else {}
        except Exception:
            cur = {}
        if new is None:
            return cur.get(sess, 0)
        cur[sess] = new
        try:
            json.dump(cur, open(self._CURSOR_FILE, "w"))
        except Exception:
            pass

    _HARVEST_LOG = os.path.join(AUT, ".harvest.log")

    def _hlog(self, r: dict, sess: str):
        """Durable trace of every harvest. A run that learned NOTHING and a hook that never
        FIRED both printed nothing before, so the two were indistinguishable — that hole hid a
        dead hook for five days. One JSON line per call is the signal that tells them apart."""
        try:
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": sess,
                   "new": r.get("new", 0), "learned": r.get("learned", []),
                   "error": r.get("error"), "reason": r.get("reason")}
            with open(self._HARVEST_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def harvest(self, hook: dict) -> dict:
        """Called by a Stop hook after each turn: distill any new craft lesson, log the outcome."""
        r = self._harvest(hook)
        self._hlog(r, (hook or {}).get("session_id") or "default")
        return r

    def _harvest(self, hook: dict) -> dict:
        """Distill any new craft lesson from the fresh conversation turns and self-write it.
        Fail-open — never breaks the turn. (harvest() wraps this to record the outcome.)"""
        tp = (hook or {}).get("transcript_path")
        sess = (hook or {}).get("session_id") or "default"
        if not tp or not os.path.exists(tp):
            return {"new": 0, "reason": "no transcript"}
        lines = open(tp, encoding="utf-8").read().splitlines()
        start = self._cursor(sess)
        turns = []
        for ln in lines[start:]:
            try:
                role, txt = self._turn_text(json.loads(ln))
            except Exception:
                continue
            if txt:
                turns.append(f"{role.upper()}: {txt[:1600]}")
        if not turns:
            self._cursor(sess, len(lines))      # nothing new to distill — safe to skip ahead
            return {"new": 0}
        existing = ", ".join(l["slug"] for l in self.st.craft_lessons())
        sys_p = (
            "You silently watch a working conversation about producing short emotional video reels "
            "(NGO / elder content). Extract ONLY durable, reusable CRAFT lessons — the owner's taste, "
            "corrections, do/don't rules, or techniques applied as improvements that would help a "
            "FUTURE reel. IGNORE status, chit-chat, one-off facts, tool mechanics, infra. Return "
            'STRICT JSON {"lessons":[{"slug":"kebab-id","title":"one-line imperative rule",'
            '"why":"one short clause"}]} — {"lessons":[]} if nothing durable, at most 2. Do NOT '
            f"duplicate these existing lessons (by concept or slug): {existing}")
        try:
            import agents
            txt, _ = agents.chat([{"role": "system", "content": sys_p},
                                  {"role": "user", "content": "\n".join(turns[-14:])}],
                                 max_tokens=1200, temperature=0)
        except Exception as e:
            # cursor NOT advanced: those turns were never distilled, so the next Stop hook
            # retries them. Fail-open must not become lose-the-lesson (gateways do go down).
            return {"new": len(turns), "error": str(e)[:80]}
        self._cursor(sess, len(lines))          # the model answered — these turns are spent
        lessons = []
        i, j = txt.find("{"), txt.rfind("}")       # robust: tolerate preamble / truncation
        if i != -1 and j > i:
            try:
                lessons = json.loads(txt[i:j + 1]).get("lessons", [])
            except Exception:
                lessons = []
        learned = []
        for l in lessons[:2]:
            slug = (l.get("slug") or "").strip()
            if slug and l.get("title"):
                self.st.learn_lesson(slug=slug, title=l["title"][:200], why=(l.get("why") or "")[:300],
                                     tier="judgment", severity="warn", source="auto-harvest")
                learned.append(slug)
        return {"new": len(turns), "learned": learned}




def _print(r: dict):
    def src(b):                                 # how fresh is this credit number?
        if b.get("source") != "observed":
            return ", manual"                   # entered at signup, never seen on the site
        age = b.get("read_ago_min")
        return f", read {age}m ago" if age is not None else ""

    print(f"[brain] {r['brain']}  —  {'HEALTHY' if r['healthy'] else 'NEEDS ATTENTION'}")
    print(f"  work:    {r['work'] or 'none'}   (mid-flight to resume: {r['resume_backlog']})")
    print(f"  sources: {len(r['budget_now'])} ready · {len(r['depleted'])} depleted · {len(r['cooling'])} cooling")
    for b in r["budget_now"]:
        print(f"             ready  {b['site']}/{b['account']}  ({b['remaining']} cr{src(b)})")
    for b in r["cooling"]:
        print(f"             cool   {b['site']}/{b['account']}  ({b['cooldown_min']}m left{src(b)})")
    k = r["knowledge"]
    print(f"  learned: {k['craft_lessons']} craft lessons ({k['gating']} gating) · "
          f"{k['reels_fingerprinted']} reels fingerprinted · {k.get('candidates_pending', 0)} proposed")
    print("  reels:   " + (", ".join(f"{x['project']}[{x['qc']}]" for x in r.get("reels", []))
                          or "none built"))
    for n in r["needs"]:
        print(f"  NEEDS:   {n}")


def _selfcheck():
    global PROJECTS
    import tempfile
    # hermetic: point the report at an empty temp projects dir so assertions don't depend on
    # whatever reels (or leftover test dirs) happen to be in the workspace.
    PROJECTS = os.path.join(tempfile.mkdtemp(), "projects")
    os.makedirs(PROJECTS)
    # ...and the clips library too: absorb() now reads narration out of the SOURCE CLIPS, so a
    # fixture that wrote them under the real clips/ would pollute the live library.
    qc._CLIPS_ROOT = os.path.join(tempfile.mkdtemp(), "clips")
    os.makedirs(qc._CLIPS_ROOT)
    st = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    b = LocalBrain(st=st)
    r = b.report()
    assert r["healthy"] and not r["work"], "fresh brain: healthy, no work"
    st.add_job("demo", 0, "a lantern in fog")                 # one queued, no credit anywhere
    st.set_credits("flow", "a1", remaining=0, resets_at=time.time() + 9999)   # dry + not due
    r = b.report()
    assert r["work"].get("queued") == 1, r["work"]
    assert any("pending but every source" in n for n in r["needs"]), r["needs"]
    assert not r["healthy"], "queued work + no source => needs attention"
    st.set_credits("flow", "a1", remaining=5)                 # now a source is ready
    assert LocalBrain(st=st).report()["healthy"], "ready source clears the flag"
    # credit numbers say WHERE they came from: manual until a run observes the real one
    f = next(x for x in LocalBrain(st=st).report()["budget_now"] if x["site"] == "flow")
    assert f["source"] == "manual", "a hand-entered balance must be labelled manual"
    st.observe_credits("flow", "a1", 7)
    f = next(x for x in LocalBrain(st=st).report()["budget_now"] if x["site"] == "flow")
    assert f["source"] == "observed" and f["remaining"] == 7 and f["read_ago_min"] == 0, f
    # ONE SOURCE: a reel locks to the site its first clip came from, so a dry lock makes it WAIT
    # by design. Because OTHER sites still have credit, the generic "every source depleted" flag
    # stays quiet — the reel would stall forever behind a healthy-looking report. Name it.
    _lk = st.add_job("locked", 0, "clip one")
    st.set_status(_lk, "done", site="krea", video_path="/tmp/one.mp4")
    st.add_job("locked", 1, "clip two")
    st.set_credits("krea", "a1", remaining=0)
    rep = b.report()
    assert any("waiting on their one source" in n and "locked" in n for n in rep["needs"]), rep["needs"]
    assert not rep["healthy"], "a stalled reel is not a healthy brain"
    st.set_credits("krea", "a1", remaining=4)
    assert not any("locked" in n for n in b.report()["needs"]), "topping up the source clears it"
    b.learn(slug="demo-rule", title="t", tier="judgment", severity="warn")
    assert b.report()["knowledge"]["craft_lessons"] == 1
    assert b.directive({"op": "report"})["brain"] == LocalBrain.name
    try:
        b.directive({"op": "launch_nukes"}); assert False
    except NotImplementedError:
        pass
    # sensing + reflect: events accrue, reflect distills them into one episodic note
    b.observe({"tool_name": "Edit", "session_id": "S",
               "tool_input": {"file_path": os.path.join(ROOT, "projects", "demo", "build.py")}})
    b.observe({"tool_name": "Bash", "session_id": "S", "tool_input": {"command": "python3 assemble.py"}})
    r = b.reflect({"session_id": "S"})
    assert r["events"] == 2 and "demo" in r["projects"], r
    assert not [e for e in st.events(session="S", processed=False) if e["kind"] != "session"], \
        "raw events marked processed after reflect"
    assert b.sessions(), "a distilled session note persists as episodic memory"
    # pattern mining -> candidate -> owner vetting gate -> craft lesson
    for _ in range(WALL_MIN):
        st.record_lesson("kling", "captcha", account="a1")
    assert "wall-kling-captcha" in b.propose(), "repeated captchas on kling -> a candidate"
    assert any(c["slug"] == "wall-kling-captcha" for c in st.candidates()), "candidate is pending"
    assert not any(l["slug"] == "wall-kling-captcha" for l in st.craft_lessons()), \
        "a candidate must NOT reach the QC gate before the owner promotes it (the vetting gate)"
    b.promote("wall-kling-captcha")
    assert any(l["slug"] == "wall-kling-captcha" for l in st.craft_lessons()), "promote -> real craft lesson"
    assert not st.candidates(status="pending"), "a promoted candidate leaves the pending queue"
    # in-session repeated failures (sensed via PostToolUseFailure) also mine into a candidate
    for _ in range(FAIL_MIN):
        b.observe({"tool_name": "Bash", "session_id": "F", "tool_input": {"command": "python3 render_veo.py"}},
                  failed=True)
    assert any(s.startswith("cmdfail-") for s in b.propose()), "a repeated failing command -> a candidate"
    assert any(c["slug"].startswith("cmdfail-") for c in st.candidates()), "the fail candidate is pending"
    # autonomous conversation harvest: new transcript turns -> LLM -> craft lesson self-written
    import tempfile as _tf
    import subprocess
    import agents as _ag
    _orig_chat = _ag.chat
    tdir = _tf.mkdtemp()
    tp = os.path.join(tdir, "t.jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "the white caption box looks fake, blend it"}]}}) + "\n")
    b._CURSOR_FILE = os.path.join(tdir, "cur.json")
    b._HARVEST_LOG = os.path.join(tdir, "harvest.log")
    _ag.chat = lambda msgs, **k: ('{"lessons":[{"slug":"blend-captions","title":"Blend captions","why":"boxes look fake"}]}', "stub")
    try:
        r = b.harvest({"transcript_path": tp, "session_id": "H"})
        assert "blend-captions" in r.get("learned", []), r
        assert any(l["slug"] == "blend-captions" for l in st.craft_lessons()), "harvested lesson is written"
        assert b.harvest({"transcript_path": tp, "session_id": "H"})["new"] == 0, "cursor skips already-seen turns"
        # every call leaves a trace, including one that learned NOTHING — a silent no-op and a
        # hook that never fired used to look identical, which is how a dead hook hid for days
        logged = [json.loads(l) for l in open(b._HARVEST_LOG, encoding="utf-8")]
        assert len(logged) == 2 and logged[0]["learned"] == ["blend-captions"], logged
        assert logged[1]["new"] == 0 and logged[1]["session"] == "H", logged[1]
        # a gateway failure must NOT burn those turns: cursor stays put, the next hook retries
        _ag.chat = lambda msgs, **k: (_ for _ in ()).throw(RuntimeError("gateway down"))
        err = b.harvest({"transcript_path": tp, "session_id": "H2"})
        assert "error" in err, err
        _ag.chat = lambda msgs, **k: ('{"lessons":[{"slug":"retry-lesson",'
                                      '"title":"Retry works","why":"cursor held on failure"}]}', "stub")
        ok = b.harvest({"transcript_path": tp, "session_id": "H2"})
        assert ok.get("new") == 1 and "retry-lesson" in ok.get("learned", []), \
            f"a failed harvest must be retried, not skipped: {ok}"
    finally:
        _ag.chat = _orig_chat
    # report() sees BOTH final names; a persisted HELD verdict becomes a need
    os.makedirs(os.path.join(PROJECTS, "reel-a"))
    open(os.path.join(PROJECTS, "reel-a", "final.mp4"), "wb").write(b"x")
    r = b.report()
    assert [x["project"] for x in r["reels"]] == ["reel-a"] and r["deliverables"] == ["reel-a"], r["reels"]
    assert r["reels"][0]["qc"] == "unchecked", "a never-gated reel is 'unchecked', not HELD"
    assert not any("HELD" in n for n in r["needs"]), r["needs"]
    st.record_qc("reel-a", {"ok": False, "block": ["no audio"], "gate": [],
                            "warn": [], "checklist": []})
    r = b.report()
    assert r["reels"][0]["qc"] == "held" and r["reels"][0]["block"] == 1, r["reels"]
    assert any("HELD by the craft gate" in n for n in r["needs"]), r["needs"]
    os.makedirs(os.path.join(PROJECTS, "reel-b"))
    open(os.path.join(PROJECTS, "reel-b", "final_veo.mp4"), "wb").write(b"x")
    assert set(b.report()["deliverables"]) == {"reel-a", "reel-b"}, \
        "both final names (runner + craft path) are deliverables"
    try:
        b.review("no-such-reel"); assert False, "reviewing a missing project must fail loudly"
    except SystemExit:
        pass
    # the exact user-facing path: `brain.py review <dir>` -> qc.run -> verdict persisted
    qc.seed(st)                       # a fresh brain carries the canonical auto checks
    d = os.path.join(PROJECTS, "reel-c")
    os.makedirs(d)
    open(os.path.join(d, "final.mp4"), "wb").write(b"not a real video")
    v = b.review(d)
    assert not v["ok"] and any("frozen still" in x for x in v["block"]), v["block"]
    q = st.qc_verdicts("reel-c")
    assert q and q[0]["ok"] == 0, "review persists its verdict to state.db"
    rc = next(x for x in b.report()["reels"] if x["project"] == "reel-c")
    assert rc["qc"] == "held" and rc["block"] >= 1, rc

    # ---- the continuous self-learning loop: absorb() audits what a reel actually SHIPPED ----
    bare = os.path.join(PROJECTS, "reel-bare")
    os.makedirs(bare, exist_ok=True)
    open(os.path.join(bare, "final.mp4"), "wb").write(b"x")          # a reel with NO craft
    r = b.absorb(bare)
    # narration is the one layer that CANNOT be judged from a reel with no clips on disk — the
    # measurement lives in the clips, so a clip-less project is not evidence of a narration gap
    assert r["ok"] and set(r["learned"]) == {"reel-needs-captions", "reel-needs-identity",
                                             "reel-needs-moral", "reel-needs-score"}, r
    pend = {c["slug"] for c in st.candidates(status="pending")}
    assert {"reel-needs-captions", "reel-needs-identity", "reel-needs-moral",
            "reel-needs-score"} <= pend, f"each gap must be filed as evidence: {pend}"
    gen = [e for e in st.events(limit=2000) if e["kind"] == "generation"]
    assert gen and "reel-bare" in gen[-1]["detail"], "each generation is written to memory"
    # a reel that ships EVERY layer must file nothing — the loop must be quiet when there's no gap
    full = os.path.join(PROJECTS, "reel-full")
    os.makedirs(full, exist_ok=True)
    open(os.path.join(full, "final.mp4"), "wb").write(b"x")
    for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png", "music_raw.mp3"):
        open(os.path.join(full, _f), "wb").write(b"x")
    # identity.json is READ (Reel parses it), so the fixture has to be real JSON, not a stub byte
    json.dump({"opener": "a held breath", "font": "rounded", "quote": "Kabir",
               "structure": "bookend"}, open(os.path.join(full, "identity.json"), "w"))
    # narration now lives in the CLIPS, so the fixture needs clips that carry audio — a vo/ dir
    # would no longer prove anything, which is exactly the correction this loop absorbed
    _fullclips = os.path.join(qc._CLIPS_ROOT, "reel-full")
    os.makedirs(_fullclips, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=3", "-pix_fmt", "yuv420p",
                    "-shortest", os.path.join(_fullclips, "clip_000.mp4")],
                   check=True, capture_output=True)
    r2 = b.absorb(full)
    assert r2["ok"] and r2["learned"] == [], f"a complete reel must file nothing: {r2}"
    # ...but the SAME reel with silent clips is a real gap: the scenes ship no speech at all
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=3",
                    "-pix_fmt", "yuv420p", os.path.join(_fullclips, "clip_000.mp4")],
                   check=True, capture_output=True)
    assert b.absorb(full)["learned"] == ["reel-needs-narration"], \
        "clips that carry no audio are a narration gap, not a pass"
    assert not b.absorb(os.path.join(PROJECTS, "no-such-reel"))["ok"], \
        "absorbing a missing project must fail open, not raise"
    print("OK: absorb() audits every generation, files each missing craft layer as evidence, "
          "stays quiet when complete, and fails open")

    # the owner's corrections must be teachable from the CLI and land in the enforced lesson set
    b.learn(slug="cli-learned-rule", title="A rule taught from the CLI",
            why="argv slicing must not swallow the slug", tier="judgment", severity="block",
            source="selfcheck")
    assert any(l["slug"] == "cli-learned-rule" for l in st.craft_lessons()), \
        "brain.learn() must reach the durable lesson set"
    print("OK: brain learn() records an owner correction into the enforced craft lessons")

    st.close()
    print("OK: report aggregates work/budget/knowledge; needs-flags fire; directive is read-only")
    print("OK: credit numbers are labelled manual vs observed with their read age")
    print("OK: observe senses events, reflect distills them into a session note (owner-vetted)")
    print("OK: propose mines walls + job + in-session failures -> candidates; promote gates them in")
    print("OK: harvest self-writes craft lessons from new conversation turns (no approval, advisory)")
    print("OK: failed harvest keeps its cursor (retried); both final names reported; HELD is a need")
    print("OK: brain review runs the gate, persists HELD, and report() surfaces it")



def _stdin_json() -> dict:
    """Hook payload arrives as JSON on stdin; return {} when run by hand (a tty)."""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            return json.loads(raw) if raw.strip() else {}
    except Exception:
        pass
    return {}


def main(argv):
    if "--selfcheck" in argv:
        return _selfcheck()
    cmd = argv[0] if argv else ""
    # hook-driven commands must NEVER fail the tool call / turn that triggered them
    if cmd in ("observe", "reflect", "harvest"):
        try:
            b = LocalBrain()
            if cmd == "observe":
                b.observe(_stdin_json(), failed=("fail" in argv[1:]))
            elif cmd == "reflect":
                r = b.reflect(_stdin_json())
                if r.get("events"):
                    tail = f" · {', '.join(r['projects'])}" if r.get("projects") else ""
                    print(f"[brain] reflected {r['events']} events{tail}")
            else:  # harvest
                r = b.harvest(_stdin_json())
                if r.get("learned"):
                    print(f"[brain] learned {len(r['learned'])} craft lesson(s): {', '.join(r['learned'])}")
            b.close()
        except Exception as e:
            print(f"[brain] {cmd} skipped: {e}", file=sys.stderr)
        return
    b = LocalBrain()
    try:
        if cmd == "review" and len(argv) >= 2:
            qc._report(b.review(argv[1]))
        elif cmd == "absorb" and len(argv) >= 2:
            r = b.absorb(argv[1])
            if not r.get("ok"):
                print(f"absorb skipped: {r.get('reason') or r.get('error')}")
            else:
                lay = b._layers(argv[1])
                print(f"absorbed '{r['reel']}': captions={len(lay['captions'])} "
                      f"narration={len(lay['narration'])}/{lay['clips']} clips "
                      f"moral={lay['moral']} score={lay['score']} identity={lay['identity']}")
                if r["learned"]:
                    print(f"  filed {len(r['learned'])} gap(s) as candidates: "
                          + ", ".join(r["learned"]))
                else:
                    print("  every craft layer present — nothing to file")
        elif cmd == "generations":
            evs = [e for e in b.st.events(limit=5000) if e["kind"] == "generation"]
            for e in evs[-20:]:
                try:
                    f = json.loads(e["detail"])
                    lay = f["layers"]
                    print(f"  {f['when']}  {f.get('reel', '?')}  {f['final']}  "
                          f"captions={len(lay['captions'])} "
                          f"narration={len(lay.get('narration') or [])}/"
                          f"{lay.get('clips', '?')} clips "
                          f"moral={lay['moral']} score={lay['score']} "
                          f"identity={lay.get('identity', '?')}")
                except Exception:
                    print("  (unparsed)", (e["detail"] or "")[:120])
            print(f"  {len(evs)} generation(s) absorbed into memory")
        elif cmd == "learn" and len(argv) >= 3:
            import argparse
            ap = argparse.ArgumentParser(prog="brain.py learn")
            ap.add_argument("slug"); ap.add_argument("title")
            ap.add_argument("--why", default=None); ap.add_argument("--tier", default="judgment")
            ap.add_argument("--check", default=None)
            ap.add_argument("--severity", default="block")
            ap.add_argument("--source", default="owner-cli")
            # argv[0] is the verb itself, so the flags+positionals start at argv[1:]
            # (argv[2:] silently dropped the slug and argparse blamed the title)
            a = ap.parse_args(argv[1:])
            b.learn(slug=a.slug, title=a.title, why=a.why, tier=a.tier, check_id=a.check,
                    severity=a.severity, source=a.source)
            print(f"learned '{a.slug}' ({a.tier}/{a.severity}) -> enforced by the QC gate")
        elif cmd == "sessions":
            for s in b.sessions():
                print(time.strftime("%Y-%m-%d %H:%M", time.localtime(s["ts"])), "·", (s["detail"] or "")[:300])
        elif cmd == "reels":
            out = b.report()["reels"]
            for x in out:
                note = {"pass": "gate PASS", "held": f"HELD ({x['block']} block, {x['gate']} gate)",
                        "unchecked": "not gated yet — brain.py review " + x["project"]}[x["qc"]]
                print(f"  {x['project']}: {x['final']} — {note}")
            print(f"  {len(out)} finished reel(s)")
        elif cmd == "candidates":
            pend = b.st.candidates(status="pending")
            for c in pend:
                print(f"  [{c['hits']}×] {c['slug']}: {c['title']}")
                print(f"         why: {c['why']}")
            print(f"  {len(pend)} pending — promote: brain.py promote <slug> · dismiss: brain.py dismiss <slug>")
        elif cmd == "promote" and len(argv) >= 2:
            b.promote(argv[1])
            print(f"promoted '{argv[1]}' → craft lesson (now enforced by the QC gate)")
        elif cmd == "dismiss" and len(argv) >= 2:
            b.dismiss(argv[1])
            print(f"dismissed '{argv[1]}' (the miner won't re-propose it)")
        elif cmd == "--json":
            print(json.dumps(b.report(), indent=2))
        else:
            _print(b.report())
    finally:
        b.close()


if __name__ == "__main__":
    main(sys.argv[1:])


