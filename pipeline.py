"""
pipeline.py — kickoff. One command: idea -> queued clips -> the runner takes over.

    python3 pipeline.py "a lantern drifting through a misty bamboo forest at night"

Agents 1 & 2 run locally (seconds) against the LLM gateways: script, then a shot
list of standalone per-clip prompts sharing one locked 'world'. Each clip becomes a
row in state.db. The runner (`python3 automation/runner.py`, started by hand when you
need a reel — there is no daemon) then drives
them across the frontier sites, learns from walls, resumes across reboots, and
stitches the finished clips into projects/<slug>/final.mp4.

Feed the WHOLE story in one prompt — agent 2 derives the world anchor + shot list
from the complete arc. Scene-by-scene breaks the shared-world continuity.
"""

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "automation"))

import agents
import music
from state import State

_DIR = os.path.dirname(os.path.abspath(__file__))

# Vids primary, everything else as overflow (owner's call, 2026-10-08): one model means one look
# — the only way "complete character consistency" holds across scenes — plus native in-scene
# Hindi and the cheapest credit. Brain.pick floats this to the front and reroutes only when Vids
# is cooling or dry, so the multi-site waterfall still exists, used as a fallback.
PRIMARY_SITE = os.environ.get("PRIMARY_SITE", "googlevids")


def _slug(title: str) -> str:
    """The reel's folder name. Every shipped reel is titled in Hindi (WAVELENGTH.md audience
    rules), and Hindi is not in a-z0-9 — so a title-only slug stripped to "", fell back to
    "project", and would have put EVERY Hindi-titled reel in projects/project/, each one
    overwriting the last. The name is hashed when nothing ASCII survives, so two different
    Hindi titles can never collide."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if s:
        return s[:48]
    raw = (title or "project").encode("utf-8")
    return "reel-" + hashlib.sha1(raw).hexdigest()[:8]


def main():
    idea = " ".join(sys.argv[1:]).strip() or input("Idea: ").strip()
    if not idea:
        sys.exit("need an idea")

    st = State()
    # The brain fires BEFORE anything is written: its learned craft rules are put in front of all
    # three agents (agents._brief), and re-seeding here means a lesson added to qc.LESSONS since
    # the last run reaches this one. This is the pre-generation trigger the owner asked for — the
    # brain used to be consulted only AFTER a reel shipped, so it could fail a reel but never
    # steer it.
    try:
        import qc as _qc
        _qc.seed(st)
        _n = len(st.film_lessons())
        print(f"brain — {_n} learned craft rule(s) primed into every agent")
    except Exception as e:  # noqa: BLE001 — no brain must never cost a reel
        print(f"brain preflight skipped ({str(e)[:100]})")

    print("agent 1 — writing script...")
    try:
        script = agents.write_script(idea)
        print("agent 2 — building shot list...")
        shot = agents.build_shotlist(script)
    except RuntimeError as e:
        # Every route down is not a bad idea — it is the machine out of options, and re-running
        # the kickoff cannot fix it. Stop loudly, with the record a person needs, instead of
        # dying as a traceback that says nothing about what to bring back up.
        import crisis
        crisis.gateway_down(e, subject=idea[:60], st=st)
        st.close()
        raise SystemExit(2)
    # Shaped for the site this reel opens its bid on (Vids/Flow get a labelled brief, the rest the
    # plain paragraph). A failover moves the WHOLE reel to one site — one reel, one source — so
    # whichever site ends up rendering it gets the same words either way.
    prompts = agents.clip_prompts(shot, PRIMARY_SITE)

    # the model writes an ASCII slug (it can transliterate; _slug cannot) — fall back to the
    # title for an older plan that predates the field, and to a hash for a Hindi-only title.
    slug = _slug(shot.get("slug") or "") if (shot.get("slug") or "").strip() else ""
    if not slug or slug == "project":
        slug = _slug(shot.get("title"))
    pdir = os.path.join(_DIR, "projects", slug)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "script.md"), "w") as f:
        f.write(f"# {shot.get('title')}\n\nIDEA: {idea}\n\n{script}")
    with open(os.path.join(pdir, "shotlist.json"), "w") as f:
        json.dump(shot, f, indent=2)

    # agent 3 — the reel's identity + its caption cards. These are the two files the owner used
    # to hand-write per reel, which is why the engine could never finish one on its own. The
    # prior identities go in so the new reel does not repeat an editing pattern (WAVELENGTH §5).
    print("agent 3 — art direction (identity + caption cards)...")
    try:
        prior = [json.loads(r["fp"]) for r in st.other_identities(slug)]
    except Exception:  # noqa: BLE001 — a first reel has nothing to compare against
        prior = []
    try:
        pres = agents.presentation(script, shot, prior=prior)
        pres["identity"]["reel"] = slug
        with open(os.path.join(pdir, "identity.json"), "w", encoding="utf-8") as f:
            json.dump(pres["identity"], f, indent=2, ensure_ascii=False)
        with open(os.path.join(pdir, "captions.json"), "w", encoding="utf-8") as f:
            json.dump(pres["captions"], f, indent=2, ensure_ascii=False)
        print(f"  identity: {pres['identity']['opener'][:70]}")
        print(f"  hook card: {' / '.join(pres['captions']['hook']['lines'])}")
    except Exception as e:  # noqa: BLE001 — a reel with no cards still renders, and the craft
        print(f"  art direction skipped ({str(e)[:130]})")   # gate will HELD it for the layer

    # The score is chosen NOW, from the reel's own mood (agents.py _SCHEMA writes music_mood /
    # music_want), so the search query is written for this reel rather than guessed — and so it
    # is on the shelf before the clips land. Library first; a search only when nothing fits.
    # Never fatal: a reel that cannot find music still ships.
    print("score — matching the bed to the reel...")
    try:
        music.choose({"mood": shot.get("music_mood")
                      or f"soft instrumental for {shot.get('title') or 'a short reel'}",
                      "want": shot.get("music_want") or "warm",
                      "min_seconds": 30}, project_dir=pdir)
    except Exception as e:  # noqa: BLE001 — a missing score must not cost the reel
        print(f"  score skipped ({str(e)[:120]})")

    # enqueue one job per clip — the durable brain owns them from here
    for i, prompt in enumerate(prompts):
        st.add_job(slug, i, prompt, site=PRIMARY_SITE)
    st.close()

    print(f"  '{shot.get('title')}': {len(prompts)} clips queued  (projects/{slug}/)")
    print(f"  primary source: {PRIMARY_SITE} (other sites only as fallback)")
    print("\nStart the render loop when you're ready (you run it — nothing runs on its own):")
    print("    python3 automation/runner.py            # headless")
    print("    python3 automation/runner.py --headed   # watch it drive the browser")
    print(f"\nThe runner assembles the finished reel itself: projects/{slug}/final_veo.mp4")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        # Every reel is titled in Hindi, and Hindi does not survive an a-z0-9 strip — the bug
        # this guards is that two different Hindi titles both became projects/project/.
        assert _slug("The Tailor's Thread") == "the-tailor-s-thread"
        assert _slug("Mixed हिंदी Title 2026") == "mixed-title-2026"
        a, b = _slug("स्मृतियों का धागा"), _slug("एक और कहानी")
        assert a.startswith("reel-") and len(a) == 13, a
        assert a != b, "two different Hindi titles must not share a folder"
        assert a == _slug("स्मृतियों का धागा"), "the fallback must be stable across runs"
        assert _slug("") != _slug("   "), "even empty titles must not collide"
        print("OK: a Hindi title gets a stable unique folder, never the shared 'project'")
    else:
        main()
