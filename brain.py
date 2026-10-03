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
    python3 brain.py review <project>  # run the craft gate on a reel
    python3 brain.py --selfcheck       # offline: prove the report aggregates correctly

Mother-brain seam (defined, NOT wired — YAGNI until the central brain exists):
  report()        -> a plain JSON-able dict = the READ port a central brain polls.
  directive(cmd)  -> the COMMAND port; stubbed to read-only ops. Wire a transport
                     here when there's something on the other end. Contract: BRAIN.md.
"""
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
        """Judgment: run the craft gate on a reel dir (delegates to qc.run)."""
        d = project if os.path.isdir(project) else os.path.join(PROJECTS, project)
        return qc.run(d, st=self.st)

    # ---- the self-report: read port for a human AND the future mother brain ----
    def report(self) -> dict:
        db, now = self.st.db, time.time()
        work = {r["status"]: r["n"]
                for r in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        budget, depleted, cooling = [], [], []
        for c in db.execute("SELECT site,account,remaining,resets_at FROM credits"):
            cd = self.st.cooldown_left(c["site"], c["account"], now)
            it = {"site": c["site"], "account": c["account"], "remaining": c["remaining"]}
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
        finals = [p for p in sorted(os.listdir(PROJECTS))
                  if os.path.exists(os.path.join(PROJECTS, p, "final_veo.mp4"))] \
            if os.path.isdir(PROJECTS) else []
        needs = []
        if work.get("failed"):
            needs.append(f"{work['failed']} job(s) failed — inspect last_error")
        if (work.get("queued") or self.st.resume()) and not budget:
            needs.append("work pending but every source is depleted or cooling — wait for reset / re-login")
        if pending:
            needs.append(f"{pending} proposed lesson(s) await review — brain.py candidates")
        return {
            "brain": self.name, "root": ROOT, "ts": now,
            "healthy": not needs,
            "work": work, "resume_backlog": len(self.st.resume()),
            "budget_now": budget, "depleted": depleted, "cooling": cooling,
            "knowledge": {"craft_lessons": len(cl), "gating": gating,
                          "reels_fingerprinted": reels, "candidates_pending": pending},
            "deliverables": finals, "needs": needs,
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

    def harvest(self, hook: dict) -> dict:
        """Called by a Stop hook after each turn: distill any new craft lesson from the fresh
        conversation turns and self-write it. Fail-open — never breaks the turn."""
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
        self._cursor(sess, len(lines))
        if not turns:
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
            return {"new": len(turns), "error": str(e)[:80]}
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
    print(f"[brain] {r['brain']}  —  {'HEALTHY' if r['healthy'] else 'NEEDS ATTENTION'}")
    print(f"  work:    {r['work'] or 'none'}   (mid-flight to resume: {r['resume_backlog']})")
    print(f"  sources: {len(r['budget_now'])} ready · {len(r['depleted'])} depleted · {len(r['cooling'])} cooling")
    for b in r["budget_now"]:
        print(f"             ready  {b['site']}/{b['account']}  ({b['remaining']} cr)")
    for b in r["cooling"]:
        print(f"             cool   {b['site']}/{b['account']}  ({b['cooldown_min']}m left)")
    k = r["knowledge"]
    print(f"  learned: {k['craft_lessons']} craft lessons ({k['gating']} gating) · "
          f"{k['reels_fingerprinted']} reels fingerprinted · {k.get('candidates_pending', 0)} proposed")
    print(f"  reels:   {', '.join(r['deliverables']) or 'none built'}")
    for n in r["needs"]:
        print(f"  NEEDS:   {n}")


def _selfcheck():
    import tempfile
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
    b.learn(slug="demo-rule", title="t", tier="judgment", severity="warn")
    assert b.report()["knowledge"]["craft_lessons"] == 1
    assert b.directive({"op": "report"})["brain"] == LocalBrain.name
    try:
        b.directive({"op": "launch_nukes"}); assert False
    except NotImplementedError:
        pass
    # sensing + reflect: events accrue, reflect distills them into one episodic note
    b.observe({"tool_name": "Edit", "session_id": "S",
               "tool_input": {"file_path": os.path.join(PROJECTS, "demo", "build.py")}})
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
    import agents as _ag
    _orig_chat = _ag.chat
    tdir = _tf.mkdtemp()
    tp = os.path.join(tdir, "t.jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "the white caption box looks fake, blend it"}]}}) + "\n")
    b._CURSOR_FILE = os.path.join(tdir, "cur.json")
    _ag.chat = lambda msgs, **k: ('{"lessons":[{"slug":"blend-captions","title":"Blend captions","why":"boxes look fake"}]}', "stub")
    try:
        r = b.harvest({"transcript_path": tp, "session_id": "H"})
        assert "blend-captions" in r.get("learned", []), r
        assert any(l["slug"] == "blend-captions" for l in st.craft_lessons()), "harvested lesson is written"
        assert b.harvest({"transcript_path": tp, "session_id": "H"})["new"] == 0, "cursor skips already-seen turns"
    finally:
        _ag.chat = _orig_chat
    st.close()
    print("OK: report aggregates work/budget/knowledge; needs-flags fire; directive is read-only")
    print("OK: observe senses events, reflect distills them into a session note (owner-vetted)")
    print("OK: propose mines walls + job + in-session failures -> candidates; promote gates them in")
    print("OK: harvest self-writes craft lessons from new conversation turns (no approval, advisory)")


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
        elif cmd == "sessions":
            for s in b.sessions():
                print(time.strftime("%Y-%m-%d %H:%M", time.localtime(s["ts"])), "·", (s["detail"] or "")[:300])
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


