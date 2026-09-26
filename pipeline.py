"""
pipeline.py — kickoff. One command: idea -> queued clips -> the runner takes over.

    python3 pipeline.py "a lantern drifting through a misty bamboo forest at night"

Agents 1 & 2 run locally (seconds) against the LLM gateways: script, then a shot
list of standalone per-clip prompts sharing one locked 'world'. Each clip becomes a
row in state.db. The runner (launchd, or `python3 automation/runner.py`) then drives
them across the frontier sites, learns from walls, resumes across reboots, and
stitches the finished clips into projects/<slug>/final.mp4.

Feed the WHOLE story in one prompt — agent 2 derives the world anchor + shot list
from the complete arc. Scene-by-scene breaks the shared-world continuity.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation"))

import agents
from state import State

_DIR = os.path.dirname(os.path.abspath(__file__))


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "project").lower()).strip("-")
    return s[:48] or "project"


def main():
    idea = " ".join(sys.argv[1:]).strip() or input("Idea: ").strip()
    if not idea:
        sys.exit("need an idea")

    print("agent 1 — writing script...")
    script = agents.write_script(idea)
    print("agent 2 — building shot list...")
    shot = agents.build_shotlist(script)
    prompts = agents.clip_prompts(shot)

    slug = _slug(shot.get("title"))
    pdir = os.path.join(_DIR, "projects", slug)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "script.md"), "w") as f:
        f.write(f"# {shot.get('title')}\n\nIDEA: {idea}\n\n{script}")
    with open(os.path.join(pdir, "shotlist.json"), "w") as f:
        json.dump(shot, f, indent=2)

    # enqueue one job per clip — the durable brain owns them from here
    st = State()
    for i, prompt in enumerate(prompts):
        st.add_job(slug, i, prompt)
    st.close()

    print(f"  '{shot.get('title')}': {len(prompts)} clips queued  (projects/{slug}/)")
    print("\nStart / resume the render loop (or let launchd do it on wake):")
    print("    python3 automation/runner.py            # headless")
    print("    python3 automation/runner.py --headed   # watch it drive the browser")
    print(f"\nFinal lands at: projects/{slug}/final.mp4")


if __name__ == "__main__":
    main()
