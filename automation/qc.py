"""
qc.py — the craft gate. state.py stores WHAT the owner taught (craft_lessons);
this runs the checks over a finished reel and returns a verdict before hand-off.

Two tiers, straight from the lesson rows:
  auto      a check_id here in CHECKS can look at the render and pass/fail it.
  judgment  no code can see it (style drift, emotional beats) -> a checklist for eyes.

    python3 qc.py <project_dir>      # gate one reel; exit 1 if anything blocks
    python3 qc.py --seed            # (re)absorb the canonical lessons into state.db
    python3 qc.py --selfcheck       # offline: prove each check catches its failure

A blocking failure means DO NOT SHIP. A publish-gate means don't go_live yet (e.g.
music not license-cleared). The judgment checklist is for the human before approval.
"""

import glob
import json
import os
import subprocess
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
from state import State  # noqa: E402


# ---- auto checks: check_id -> fn(reel) -> list[problem strings] (empty = pass) ---------------

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _frozen_still(path):
    """True if every sampled frame is byte-identical — a valid file that plays as a still."""
    import hashlib
    d = _duration(path)
    seen = set()
    for frac in (0.1, 0.35, 0.6, 0.85):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{d*frac:.3f}", "-i", path,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        seen.add(hashlib.md5(r.stdout).hexdigest())
    return len(seen) < 2


def _duration(path):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1", path]).stdout.strip()
    return float(out or 0.0)


def check_playable(reel):
    """The video-playback-verify-gate rule, re-run at the fleet level: a file that
    ffprobe accepts can still freeze to a still + silence in QuickTime."""
    if not reel.final:
        return ["no final video found (final_veo.mp4 / final.mp4)"]
    probs = []
    if _frozen_still(reel.final):
        probs.append("video is a frozen still (all sampled frames identical)")
    has_a = "audio" in _run(["ffprobe", "-v", "error", "-select_streams", "a",
                             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                             reel.final]).stdout
    if not has_a:
        probs.append("no audio stream")
    else:
        db = -999.0
        for ln in _run(["ffmpeg", "-hide_banner", "-i", reel.final, "-af", "volumedetect",
                        "-f", "null", "-"]).stderr.splitlines():
            if "mean_volume:" in ln:
                db = float(ln.split("mean_volume:")[1].split("dB")[0])
        if db < -60:
            probs.append("audio track is silent")
    return probs


def check_caption_safe_area(reel):
    """On-screen text must clear the bottom 15% and far-right edge — the IG app paints
    the handle / caption / audio row / action buttons there. Captions are full-frame RGBA
    overlays, so the opaque bbox of each PNG is exactly where its text lands."""
    import numpy as np
    from PIL import Image
    # Look only at SOLID glyph ink (alpha > 200): a full-width semi-transparent scrim behind
    # the text (the veo-baked-text-artifact mask, ~alpha 100-140) is intentional, not "text in
    # the UI zone". ponytail: alpha threshold, not a scrim mask — revisit if a design ever uses
    # a fully-opaque full-bleed band behind text.
    INK = 200
    probs = []
    for png in reel.captions:
        im = Image.open(png).convert("RGBA")
        w, h = im.size
        a = np.asarray(im.getchannel("A"))
        ys, xs = np.where(a > INK)
        if len(ys) == 0:
            continue                                  # transparent card, no text
        name = os.path.basename(png)
        if ys.max() > h * 0.85:                       # bottom 15% is UI-covered
            probs.append(f"{name}: text reaches y={ys.max()} of {h} (past the bottom-15% line)")
        if xs.max() > w * 0.97:
            probs.append(f"{name}: text touches the far-right edge (x={xs.max()} of {w})")
    return probs


def check_realism(reel):
    """Vision review of the FINISHED reel: sample frames across it and have the brain's eyes
    (vision.review_frame) flag realism defects / AI artifacts — bodies merging, extra/missing
    limbs, duplicated objects, garbled baked-in text, style drift — BEFORE it reaches the owner.
    If the vision gateway is down it says so (does NOT silently pass)."""
    if not reel.final:
        return []
    import vision
    d = _duration(reel.final)
    if d <= 0:
        return []
    seen, answered = {}, 0
    for frac in (0.08, 0.18, 0.30, 0.42, 0.54, 0.66, 0.78, 0.90):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{d * frac:.3f}", "-i", reel.final,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if not r.stdout:
            continue
        res = vision.review_frame(r.stdout)
        if res is None:                               # gateway failed on this frame
            continue
        answered += 1
        for df in res:
            seen.setdefault(df["type"], []).append((round(d * frac, 1), df["detail"]))
    if answered == 0:
        return ["visual review could NOT run (vision gateway unavailable) — eyeball before go_live"]
    probs = []
    for t, hits in seen.items():
        ts = ", ".join(f"{tt}s" for tt, _ in hits[:3])
        probs.append(f"{t} @ {ts}: {hits[0][1]}")
    return probs


CHECKS = {"playable": check_playable, "caption-safe-area": check_caption_safe_area,
          "realism": check_realism}


# ---- canonical craft lessons: the owner's memory rules, absorbed into state.db --------------
# Editing these + `python3 qc.py --seed` re-teaches the gate. The DB is the live source of
# truth (owner can add/refine rows there); this list only bootstraps a fresh brain.
LESSONS = [
    dict(slug="video-playback", check_id="playable", severity="block",
         title="The render must actually PLAY (not a frozen still / silent)",
         why="a valid-to-ffprobe file can freeze to a still + silence in QuickTime"),
    dict(slug="caption-safe-area", check_id="caption-safe-area", severity="block",
         title="On-screen text clears the bottom 15% and far-right edge",
         why="the IG app paints the handle, caption, audio row and action buttons there"),
    dict(slug="visual-realism", check_id="realism", severity="publish-gate",
         title="The finished reel passes a vision realism review (no body-merge, extra limbs, "
               "duplicated objects, garbled baked text, or style drift)",
         why="AI reels ship artifacts the owner should not have to catch (a figure walking INTO "
             "another, meaningless baked Hindi text) — the brain reviews every reel by eye (vision) "
             "before hand-off, using its past-artifact knowledge"),
    dict(slug="unique-reel-identity", tier="judgment", severity="block",
         title="A fresh identity every reel — opener, fonts, transitions, quote, structure",
         why="repeating an old reel's look is the flaw the owner caught before"),
    dict(slug="style-consistency", tier="judgment", severity="block",
         title="No photoreal<->animated drift; one art-style anchor bound to every character",
         why="style misdirection is AI video's #1 flaw"),
    dict(slug="editing-preserve-essence", tier="judgment", severity="publish-gate",
         title="Never trim an emotional beat (even a silent one); cut by viewing frames",
         why="silencedetect-driven cuts kill the beat the reel exists for"),
    dict(slug="veo-narration-not-lipsync", tier="judgment", severity="publish-gate",
         title="VO stays true background narration (faces from behind/wide + VO muxed separately)",
         why="Veo lip-syncs any VO line to whatever face is on camera"),
    dict(slug="background-score-workflow", tier="judgment", severity="publish-gate",
         title="Ask before adding music; royalty-free, ducked under dialogue",
         why="the owner picks the track (I can't hear audio)"),
    dict(slug="music-license", tier="judgment", severity="publish-gate",
         title="Music license verified before go_live",
         why="reels have been publish-blocked on unverified music rights"),
]


def seed(st):
    for l in LESSONS:
        st.learn_lesson(source="memory-rules", **l)


# ---- a reel's artifacts, discovered from its project dir ------------------------------------
class Reel:
    def __init__(self, project_dir):
        self.dir = os.path.abspath(project_dir)
        self.name = os.path.basename(self.dir.rstrip("/"))
        self.final = next((os.path.join(self.dir, f) for f in ("final_veo.mp4", "final.mp4")
                           if os.path.exists(os.path.join(self.dir, f))), None)
        self.captions = sorted(glob.glob(os.path.join(self.dir, "caption_*.png")))
        idp = os.path.join(self.dir, "identity.json")
        self.identity = json.load(open(idp)) if os.path.exists(idp) else None


def _identity_collision(st, reel):
    """Compare this reel's declared fingerprint to every OTHER shipped reel. Flags a look
    that repeats too many distinctive choices (opener/font/transition/quote/structure)."""
    if not reel.identity:
        return None                                   # nothing declared -> handled as judgment
    mine = {(k, json.dumps(v, sort_keys=True)) for k, v in reel.identity.items()}
    if not mine:
        return None
    for row in st.other_identities(reel.name):
        try:
            other = json.loads(row["fp"])
        except Exception:
            continue
        theirs = {(k, json.dumps(v, sort_keys=True)) for k, v in other.items()}
        overlap = len(mine & theirs) / len(mine)
        # ponytail: naive shared-pair fraction; per-field weighting if it over/under-fires
        if overlap >= 0.6:
            shared = sorted(k for k, _ in (mine & theirs))
            return f"identity {overlap:.0%} shared with '{row['reel']}' (reused: {', '.join(shared)})"
    return None


def run(project_dir, st=None):
    """Gate one reel. Returns {'ok', 'block', 'gate', 'warn', 'checklist'}."""
    own = st is None
    st = st or State()
    if not st.craft_lessons():
        seed(st)                                      # bootstrap a fresh brain
    reel = Reel(project_dir)
    block, gate, warn, checklist = [], [], [], []
    bucket = {"block": block, "publish-gate": gate, "warn": warn}

    for l in st.craft_lessons():
        if l["tier"] == "auto" and l["check_id"] in CHECKS:
            for p in CHECKS[l["check_id"]](reel):
                bucket.get(l["severity"], warn).append(f"[{l['slug']}] {p}")
        elif l["tier"] == "judgment":
            checklist.append((l["severity"], f"[{l['slug']}] {l['title']} — {l['why'] or ''}"))

    coll = _identity_collision(st, reel)
    if coll:
        block.append(f"[unique-reel-identity] {coll}")
    elif reel.identity:
        st.record_identity(reel.name, json.dumps(reel.identity, sort_keys=True))
    else:
        checklist.append(("block", "[unique-reel-identity] no identity.json declared — "
                          "confirm by eye this reel's look is distinct from prior ones"))

    if own:
        st.close()
    return {"ok": not block and not gate, "block": block, "gate": gate,
            "warn": warn, "checklist": checklist, "reel": reel.name}


def _report(v):
    print(f"QC: {v['reel']}")
    for tag, items in (("BLOCK — do not ship", v["block"]),
                       ("PUBLISH-GATE — don't go_live yet", v["gate"]),
                       ("warn", v["warn"])):
        for i in items:
            print(f"  {tag}: {i}")
    if v["checklist"]:
        print("  judgment checklist (human eyes before approval):")
        for sev, line in v["checklist"]:
            print(f"    - ({sev}) {line}")
    print("  => " + ("PASS (auto checks clean; review the checklist)" if v["ok"]
                      else "HELD (fix the block/gate items above)"))


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        import tempfile
        st = State(os.path.join(tempfile.mkdtemp(), "t.db"))
        seed(st)
        assert set(l["check_id"] for l in st.craft_lessons(tier="auto")) <= set(CHECKS) | {None}, \
            "every auto lesson must map to a real check"

        # build a deliberately-bad reel: frozen silent video + a caption that sits in the UI zone
        d = tempfile.mkdtemp()
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=2",
                        "-pix_fmt", "yuv420p", os.path.join(d, "final_veo.mp4")],
                       check=True, capture_output=True)
        from PIL import Image
        im = Image.new("RGBA", (720, 1280), (0, 0, 0, 0))
        im.paste((255, 255, 255, 255), (100, 1200, 620, 1260))   # text in the bottom 15%
        im.save(os.path.join(d, "caption_bad.png"))

        # realism is a LIVE vision call in production (8 sampled frames x gateway failover x
        # 90s each). Stub the gateway so --selfcheck stays OFFLINE, then prove both directions:
        # a down gateway must NOT silently pass, and a reported defect must gate the reel.
        import vision
        _orig_review = vision.review_frame
        try:
            vision.review_frame = lambda png: None                # gateway unavailable
            v = run(d, st)
            assert any("frozen still" in b for b in v["block"]), v["block"]
            assert any("no audio" in b for b in v["block"]), v["block"]
            assert any("bottom-15%" in b for b in v["block"]), v["block"]
            assert any("visual review could NOT run" in g for g in v["gate"]), \
                "a down vision gateway must say so, not silently pass"
            assert not v["ok"], "a frozen silent reel with a bad caption must be HELD"

            vision.review_frame = lambda png: [{"type": "body-merge",
                                                "detail": "her torso overlaps his shoulder"}]
            v = run(d, st)
            assert any("body-merge" in g for g in v["gate"]), v["gate"]
            assert not v["ok"], "a reel with a vision-reported defect must be HELD"

            vision.review_frame = lambda png: []                  # every frame reviews clean
            # identity collision: a second reel that reuses an existing look must block
            st.record_identity("reel_a", json.dumps(
                {"opener": "reveal", "font": "Didot", "quote": "Ibn Battuta", "structure": "hero-hold"}))
            d2 = tempfile.mkdtemp()
            json.dump({"opener": "reveal", "font": "Didot", "quote": "Ibn Battuta", "structure": "grid"},
                      open(os.path.join(d2, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=2", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d2, "final_veo.mp4")],
                           check=True, capture_output=True)
            v2 = run(d2, st)
            assert any("unique-reel-identity" in b and "reel_a" in b for b in v2["block"]), v2["block"]

            # a clean, distinct reel passes the auto checks
            d3 = tempfile.mkdtemp()
            json.dump({"opener": "typewriter", "font": "Snell", "quote": "Rumi", "structure": "montage"},
                      open(os.path.join(d3, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=2", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d3, "final_veo.mp4")],
                           check=True, capture_output=True)
            v3 = run(d3, st)
            assert v3["ok"] and not v3["block"], v3
        finally:
            vision.review_frame = _orig_review
        st.close()
        print("OK: auto checks catch frozen/silent/off-safe-area + identity reuse; "
              "realism gates on a vision defect / reports a down gateway; clean reel passes")
    elif "--seed" in sys.argv:
        st = State(); seed(st)
        print(f"seeded {len(st.craft_lessons())} craft lessons into state.db"); st.close()
    elif len(sys.argv) > 1:
        v = run(sys.argv[1])
        _report(v)
        sys.exit(0 if v["ok"] else 1)
    else:
        print(__doc__)
