"""
crisis.py — the last rung: what happens when the machine has run out of itself.

The system is built to finish a reel without a person. It retries a flaky site, waits a cooldown
out, moves the WHOLE reel to another site before the first clip lands, re-shoots the clips a gate
rejects, and asks another model when the one it wanted is walled. This file is the other end of
that: the three places where none of it is left and the honest next move is a human.

    no-gateway       no LLM route answered — a reel cannot even be written
    clip-failed      one clip spent every retry, so its reel can never assemble
    requeue-capped   the gate rejected the same clip REQUEUE_CAP times: the feedback is not the
                     problem, and another generation is not the fix

A crisis is NOT an error. An error is handled and the run goes on; a crisis is where the run
STOPS — and stopping quietly is the one thing that must never happen. So each one leaves two
things behind, one per reader:

  * crises/<ts>-<kind>-<subject>.json — for the PERSON: what could not be done, what was already
    tried (so nobody spends an afternoon repeating it), and what is being asked of them;
  * a candidate lesson keyed by the CHECK that would have caught it — for the MACHINE. That is
    the half that makes the second one unnecessary: a person fixing a reel buys one reel, a person
    filing the check that catches it buys every reel after. It is a CANDIDATE, never a rule
    (state.propose_lesson — the brain proposes, the owner vets).

`takeover()` is the seam for when a human (or the agent acting as one) settles one: what they DID
and the CHECK they added are written into the same record, so a crisis is never closed by a
silent edit and the record says what actually fixed it.

    python3 automation/crisis.py --list
    python3 automation/crisis.py --takeover crises/<file>.json --by Claude \
        --did "re-shot clip 2 by hand" --check preflight-style-uniform
    python3 automation/crisis.py --resolve crises/<file>.json
    python3 automation/crisis.py --selfcheck
"""

import argparse
import glob
import json
import os
import re
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
for _p in (_ROOT, _DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CRISES = os.path.join(_ROOT, "crises")          # runtime state, gitignored (a to-do list, not code)

KINDS = {
    "no-gateway": "no LLM route answered — a reel cannot even be written",
    "bad-plan": "the shot list cannot be filmed as written — nothing was queued, nothing spent",
    "clip-failed": "a clip spent every retry: its reel can never assemble on its own",
    "requeue-capped": "the gate rejected the same clip twice: the feedback is not the problem",
}

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(subject: str) -> str:
    return _SLUG.sub("-", (subject or "?").lower()).strip("-")[:40] or "unknown"


def _banner(rec: dict, path: str) -> str:
    """One block a person can read without opening the JSON — and it names the file, because the
    record is the artifact, not this print."""
    out = [f"  +-- CRISIS ({rec['kind']}) -- {rec['subject']}",
           f"  |   {rec['why']}",
           f"  |   {rec['detail']}"]
    for t in rec["tried"]:
        out.append(f"  |   tried: {t}")
    if rec["check"]:
        out.append(f"  |   filed: missing-check-{rec['check']} (a candidate, not a rule)")
    out.append(f"  |   needs: {rec['needs'] or 'a person'}")
    out.append(f"  +-- {path}")
    return "\n".join(out)


def _file_check(kind: str, subject: str, detail: str, tried, check: str, st=None) -> None:
    """The machine-readable half. Keyed by the CHECK, not the reel: the same defect needing a
    person on three reels is then ONE row with hits=3 — the count is the signal that the
    generation side still does it, and nothing here changes behaviour on its own."""
    if not check:
        return
    own = st is None
    if own:
        from state import State
        st = State()
    try:
        st.propose_lesson(
            slug=f"missing-check-{check}",
            title=f"Nothing catches '{check}' before a person has to",
            why=f"The machine ran out of its own options ({kind}) on {subject}. What keeps this "
                f"out of a person's hands is a check that catches it at GENERATION time, not a "
                f"gate that finds it again after the render.",
            evidence=("; ".join(str(t) for t in tried) + " — " + (detail or ""))[:500])
    finally:
        if own:
            st.close()


def raise_crisis(kind: str, subject: str, detail: str, tried=(), check: str = "",
                 needs: str = "", st=None):
    """Record a stop; return where it was written. Never raises.

    Fail-open by contract, like the gate's bookkeeping: reporting that the machine is stuck must
    not itself be the thing that kills the run. If the record cannot be written, the banner still
    prints — a person reading it is the whole point."""
    try:
        os.makedirs(CRISES, exist_ok=True)
        ts = int(time.time())
        rec = {"ts": ts, "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
               "kind": kind, "why": KINDS.get(kind, ""), "subject": subject, "detail": detail,
               "tried": [str(t) for t in tried], "check": check, "needs": needs,
               "takeover": None}
        path = os.path.join(CRISES, f"{ts}-{kind}-{_slug(subject)}.json")
        n = 2
        while os.path.exists(path):        # two crises in one second must not lose one of them
            path = os.path.join(CRISES, f"{ts}-{kind}-{_slug(subject)}-{n}.json")
            n += 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        print(_banner(rec, path))
    except Exception as e:  # noqa: BLE001 — see the docstring: the print below is the fallback
        print(f"  +-- CRISIS ({kind}) -- {subject}: {detail}\n  |   (and the record itself could "
              f"not be written: {str(e)[:120]})")
        return None
    try:
        _file_check(kind, subject, detail, tried, check, st)
    except Exception as e:  # noqa: BLE001
        print(f"  |   (check not filed: {str(e)[:110]})")
    return path


def gateway_down(err, subject: str, st=None):
    """Every route down: the ONE failure the machine genuinely cannot route around, because the
    thing that would route is the thing that is down."""
    import agents
    return raise_crisis(
        "no-gateway", subject=subject, detail=str(err)[:300],
        tried=[f"routes tried in order: {', '.join(agents._ORDER)}",
               "cooldown escalation (routerhealth) — every route is walled at once",
               "model picks by measurement (automation/model_picks.json) — no route left to pick"],
        check="gateway-preflight",
        needs="bring one LLM gateway up (FreeLLMAPI/OmniRoute) and re-run — nothing is queued yet",
        st=st)


def open_crises() -> list:
    """(path, record) for every crisis nobody has settled, oldest first. Unreadable files are
    skipped rather than fatal — a to-do list with one bad line is still a to-do list."""
    out = []
    for p in sorted(glob.glob(os.path.join(CRISES, "*.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                out.append((p, json.load(f)))
        except (OSError, ValueError):
            continue
    return out


def takeover(path: str, by: str, did: str, check: str = "", st=None) -> dict:
    """A person (or the agent standing in for one) settled this. What they DID and the CHECK that
    now catches it go into the record — the machine reads the check, the owner reads the record."""
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    rec["takeover"] = {"by": by, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "did": did,
                       "check": check}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2, ensure_ascii=False)
    if check:
        # the check now EXISTS (the person wrote one), so this is not a missing-check candidate —
        # it is the lesson itself, filed for the record and bumped on every recurrence
        try:
            _file_check(rec["kind"], rec["subject"], f"takeover by {by}: {did}", rec["tried"],
                        check, st)
        except Exception as e:  # noqa: BLE001
            print(f"  (lesson not filed: {str(e)[:110]})")
    print(f"TAKEOVER on {os.path.basename(path)}: {by} — {did}"
          + (f"  (check: {check})" if check else ""))
    return rec


def resolve(path: str) -> None:
    """Settled and recorded -> the to-do item goes. The durable half is the lesson in the brain."""
    os.unlink(path)
    print(f"resolved {os.path.basename(path)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="every crisis nobody has settled")
    ap.add_argument("--takeover", metavar="FILE", help="record how one was settled")
    ap.add_argument("--resolve", metavar="FILE", help="drop a settled crisis from the list")
    ap.add_argument("--by"), ap.add_argument("--did"), ap.add_argument("--check")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()

    if a.selfcheck:
        import tempfile
        from state import State

        tmp = tempfile.mkdtemp()
        saved = CRISES
        globals()["CRISES"] = os.path.join(tmp, "crises")
        st = State(os.path.join(tmp, "t.db"))
        try:
            p = raise_crisis("requeue-capped", "dadi ki dhansuchi", "clip 2 rejected twice",
                             tried=["2 re-shoots"], check="preflight-style-uniform",
                             needs="a person: settle clip 2", st=st)
            assert p and os.path.exists(p), "a crisis must leave a file behind"
            rec = json.load(open(p, encoding="utf-8"))
            assert rec["kind"] == "requeue-capped" and rec["tried"] == ["2 re-shoots"], rec
            assert rec["why"] and rec["needs"] and rec["takeover"] is None
            assert "dadi-ki-dhansuchi" in os.path.basename(p), "the subject must be in the name"
            # the machine-readable half: ONE row keyed by the CHECK, and a recurrence bumps it
            rows = [r for r in st.candidates() if r["slug"] == "missing-check-preflight-style-uniform"]
            assert rows and rows[0]["hits"] == 1, st.candidates()
            raise_crisis("requeue-capped", "another reel", "same defect", check="preflight-style-uniform",
                         st=st)
            hits = [r["hits"] for r in st.candidates()
                    if r["slug"] == "missing-check-preflight-style-uniform"][0]
            assert hits == 2, f"a recurrence must be one row, not two: {hits}"
            assert len(open_crises()) == 2, "both crises are open"

            # an unknown kind still records (the label is decoration, the record is not), and a
            # crisis naming no check still records — only the lesson is skipped
            p3 = raise_crisis("weird-new-kind", "x", "d", st=st)
            assert "weird-new-kind" in os.path.basename(p3)
            # FAIL-OPEN: a dead brain must not stop the record being written
            class _Boom:
                def propose_lesson(self, **kw):
                    raise RuntimeError("database is locked")
            p4 = raise_crisis("clip-failed", "reel", "clip 1 dead", check="c", st=_Boom())
            assert p4 and os.path.exists(p4), "bookkeeping must never cost the report"

            takeover(p, by="Claude", did="re-shot clip 2 from the frames", check="preflight-style-uniform")
            assert json.load(open(p, encoding="utf-8"))["takeover"]["did"].startswith("re-shot")
            resolve(p)
            assert not os.path.exists(p) and len(open_crises()) == 3
            b = _banner(rec, "crises/x.json")
            assert "CRISIS (requeue-capped)" in b and "preflight-style-uniform" in b and \
                b.rstrip().endswith("crises/x.json"), b
        finally:
            globals()["CRISES"] = saved
            st.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        print("OK: crisis.py — a stop is recorded for the person and filed as a check for the "
              "machine, bookkeeping never kills the report, and a takeover says what fixed it")
        raise SystemExit(0)

    if a.list:
        rows = open_crises()
        print(f"{len(rows)} open crisis(es)")
        for p, r in rows:
            print(f"  [{r['kind']}] {r['subject']}: {r['detail'][:110]}\n    {p}"
                  f"\n    needs: {r['needs']}")
        raise SystemExit(0)

    if a.takeover:
        if not (a.by and a.did):
            sys.exit("--takeover needs --by and --did")
        takeover(a.takeover, by=a.by, did=a.did, check=a.check or "")
        raise SystemExit(0)

    if a.resolve:
        resolve(a.resolve)
        raise SystemExit(0)

    ap.print_help()
