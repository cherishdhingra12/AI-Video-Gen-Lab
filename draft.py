#!/usr/bin/env python3
"""draft.py — write a script WITHOUT spending a video credit.

The pipeline writes a script and then queues clips; this stops after the writing, so a brief can be
judged on paper first. It runs exactly what a kickoff runs — agents.write_script, which drafts,
critiques against STORY.md and rewrites — and writes the result plus the critique into a folder:

    python3 draft.py "an idea for a reel" [--out script-proof/dadi]

The files it writes: script.md (the script that would be filmed), script_critique.json (every
finding, the model that judged it, and whether the critic could run at all). Re-run it as often as
you like: a text call costs nothing, a render costs five clips.

    --rounds 0     draft only — no critique (what the writer used to do)
"""
import argparse
import json
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "automation"))

import agents  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("idea", help="the brief, in the owner's own words")
    ap.add_argument("--out", help="folder to write into (default: script-proof/<timestamp>)")
    ap.add_argument("--seconds", type=int, help="length to ask for; default: whatever the idea says")
    ap.add_argument("--rounds", type=int, help="critique passes; 0 drafts without judging")
    a = ap.parse_args(argv)

    if a.rounds is not None:
        agents.SCRIPT_ROUNDS = max(0, a.rounds)
    out = a.out or os.path.join(_DIR, "script-proof", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)

    print(f"writing (SCRIPT_ROUNDS={agents.SCRIPT_ROUNDS})...")
    script = agents.write_script(a.idea, seconds=a.seconds)
    crit = dict(agents.LAST_CRITIQUE)

    import script_enforcer
    enf = script_enforcer.check(script, a.idea, require_critic=False)

    with open(os.path.join(out, "script.md"), "w", encoding="utf-8") as f:
        f.write(f"IDEA: {a.idea}\n\n{script}")
    with open(os.path.join(out, "script_critique.json"), "w", encoding="utf-8") as f:
        json.dump(crit, f, indent=2, ensure_ascii=False)
    with open(os.path.join(out, "script_enforcer.json"), "w", encoding="utf-8") as f:
        json.dump(enf, f, indent=2, ensure_ascii=False)

    print(f"\n{out}/script.md")
    if crit.get("could_not_run"):
        print(f"! the critic could not run: {crit['could_not_run']}")
    for i, p in enumerate(crit.get("problems") or [], 1):
        print(f"  {i}. {p}")
    if not crit.get("problems") and crit.get("rounds"):
        print("  the critic found nothing to change")
    if enf.get("problems"):
        print(f"\nscript enforcer ({enf['status']}):")
        for i, p in enumerate(enf["problems"], 1):
            print(f"  * {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())