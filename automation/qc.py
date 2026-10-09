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

class Unreviewed(str):
    """A check that could NOT run (its gateway was down) — not a defect, and not a pass.
    The three vision checks share the gateway that the QC frame-review exhausts, so an outage
    used to read exactly like a craft failure and HELD a clean, finished reel (darzi-ka-tukda,
    2026-10-10: all vision routes rate-limited, every offline check green). Wrapping the
    message lets run() tell the two apart: a defect blocks the ship, an outage only gates
    go_live and demands a human look. A str subclass so it prints and greps as the message."""

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


def _has_audio(path):
    return "audio" in _run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            path]).stdout


def check_playable(reel):
    """The video-playback-verify-gate rule, re-run at the fleet level: a file that
    ffprobe accepts can still freeze to a still + silence in QuickTime."""
    if not reel.final:
        return ["no final video found (final_veo.mp4 / final.mp4)"]
    probs = []
    if _frozen_still(reel.final):
        probs.append("video is a frozen still (all sampled frames identical)")
    if not _has_audio(reel.final):
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
        return [Unreviewed("visual review could NOT run (vision gateway unavailable) — "
                           "eyeball before go_live")]
    probs = []
    for t, hits in seen.items():
        ts = ", ".join(f"{tt}s" for tt, _ in hits[:3])
        probs.append(f"{t} @ {ts}: {hits[0][1]}")
    return probs


# Where the generated clips live (clips/<project>/clip_*.mp4). Module-level and env-overridable
# so the self-check can point it at a temp dir instead of the real library.
_CLIPS_ROOT = os.environ.get(
    "AIVGL_CLIPS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clips"))


def check_craft_layers(reel):
    """A reel is not done because it PLAYS — it is done because it SHIPS the craft. The owner
    was taught captions / narration / a moral / a score more than once and reels still went out
    without them, because nothing machine-checked it. This turns them into ONE gate: the
    artifact either exists or the reel is HELD. Measured from the built project, so a renamed
    file can't talk its way past it.

    Narration is the odd layer: it must come OUT of the generated clips, never from a track
    added at assembly (WAVELENGTH.md §3 — the owner's rule). So what gets checked is that the
    SOURCE clips carry audio; a vo/ dir is no longer evidence of anything."""
    if not reel.final:
        return []                                   # playable() already owns 'no video'
    probs = []
    if not reel.captions:
        probs.append("no caption cards (caption_*.png) — the reel is unreadable muted")
    clips = sorted(glob.glob(os.path.join(_CLIPS_ROOT, reel.name, "clip_*.mp4")))
    # ponytail: no clips on disk (swept after publish) means nothing to inspect, so this passes
    # vacuously. Cheap to keep, and the alternative is an ASR call per reel.
    if clips and not any(_has_audio(c) for c in clips):
        probs.append("the source clips carry no audio — narration has to be spoken IN the scene, "
                     "not laid over it at assembly")
    if not any("moral" in os.path.basename(c).lower() for c in reel.captions):
        probs.append("no moral card (caption_moral.png) — the reel needs a stated takeaway")
    if not glob.glob(os.path.join(reel.dir, "music_raw.mp3")):
        probs.append("no background score (music_raw.mp3) — the reel plays silent/flat")
    return probs


_HOUSE_MEDIUM = "3d"        # WAVELENGTH.md §1 — every NEW reel is 3-D animation


def _declared_medium(reel) -> str:
    """A reel's own declared medium from identity.json, else the house default. Declaring
    'photorealistic warm documentary' (three shipped reels do, by design) makes the gate check
    that reel against photoreal; declaring nothing inherits the house 3-D rule."""
    s = ((reel.identity or {}).get("visual_style") or "").lower()
    if any(k in s for k in ("photoreal", "live action", "documentary", "real footage")):
        return "photoreal"
    return _HOUSE_MEDIUM


def check_style_uniform(reel):
    """One art style across the WHOLE reel. review_frame catches a frame that breaks its OWN
    look (limbs, merges, text), but a reel can be frame-perfect photoreal in scene 1 and
    animated from scene 2 — every individual frame is 'fine', the reel is inconsistent. That is
    the defect the owner actually reported, and it needs a check that compares ACROSS the reel.
    Classifies a frame's medium with vision and fails when the reel mixes media.
    """
    if not reel.final:
        return []
    import vision
    d = _duration(reel.final)
    if d <= 0:
        return []
    samples = []
    for frac in (0.06, 0.30, 0.55, 0.80, 0.95):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{d * frac:.3f}", "-i", reel.final,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if r.stdout:
            samples.append(vision.medium_of(r.stdout))
    # The expected medium is the reel's OWN declaration when it has one, else the house default
    # (3-D, WAVELENGTH.md §1). Reading it from identity.json is what lets the house 3-D rule bind
    # NEW reels without retroactively holding the three older photoreal ones — each is checked
    # against what it declared. (A bare hard-coded '3d' here once held six shipped deliverables.)
    verdict, ev = vision.medium_verdict(samples, expected=_declared_medium(reel))
    if verdict == "unknown":
        return [Unreviewed("style classification could NOT run (vision gateway unavailable) — "
                           "confirm by eye the reel stays in ONE art style")]
    if verdict == "drift":
        return [f"the reel is not in ONE art style ({ev}) — no photoreal scenes in front "
                "of animated ones"]
    if verdict == "wrong-medium":
        return [f"{ev} — WAVELENGTH.md §1: every new reel is 3-D animation"]
    return []


def _contact_sheet(frames):
    """Tile sampled frames into ONE image. The vision call takes one image, and 'same person in
    every panel?' is a question no single frame can answer."""
    try:
        import io
        from PIL import Image
        cols = 3 if len(frames) > 2 else len(frames)
        rows = (len(frames) + cols - 1) // cols
        cw, ch = 300, 533
        sheet = Image.new("RGB", (cols * cw, rows * ch), (0, 0, 0))
        for i, png in enumerate(frames[:cols * rows]):
            sheet.paste(Image.open(io.BytesIO(png)).convert("RGB").resize((cw, ch)),
                        ((i % cols) * cw, (i // cols) * ch))
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — an unbuildable sheet means 'cannot review'
        return None


def check_character_consistency(reel):
    """The owner asked for 'complete character consistency' across scenes. The clip PROMPTS carry
    a character lock, but a lock in a prompt is not evidence about the RENDER — a face can drift
    while every prompt dutifully names the same person. So this reads the frames: one panel per
    clip, tiled, and one question — is this the same person in every panel?

    Reads the SOURCE clips (no caption cards overlaid to confuse the comparison), and follows
    craft-layers' convention: clips swept after publish means there is nothing to inspect."""
    if not reel.final:
        return []
    clips = sorted(glob.glob(os.path.join(_CLIPS_ROOT, reel.name, "clip_*.mp4")))
    if len(clips) < 2:
        return []                                   # one panel cannot disagree with itself
    frames = []
    for c in clips[:6]:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{_duration(c) * 0.5:.3f}", "-i", c,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if r.stdout:
            frames.append(r.stdout)
    if len(frames) < 2:
        return []
    import vision
    sheet = _contact_sheet(frames)
    if sheet is None:
        return []
    res = vision.compare_character(sheet)
    if res is None:
        return [Unreviewed("character consistency could NOT be reviewed (vision gateway "
                           "unavailable) — check by eye that the same face and hair appear in "
                           "every scene")]
    if not res["consistent"]:
        return [f"the recurring character changes between scenes: "
                f"{res['detail'] or 'the panels do not show the same person'}"]
    return []


def check_pacing(reel):
    """Is the reel's RHYTHM usable? Deliberately not a taste check — it flags only what is
    broken whatever the content: a shot too brief to register, or a length a feed cannot carry.
    (Mixed shot lengths, a long hero hold, and a silent beat are all deliberate — see the
    editing rules — so none of those are flagged here.)"""
    if not reel.final:
        return []
    probs = []
    total = _duration(reel.final)
    if 0 < total < 10:
        probs.append(f"the reel is only {total:.1f}s — too short to land a story with a moral")
    elif total > 150:
        probs.append(f"the reel is {total:.1f}s — far past what a feed will carry; the "
                     "drop-off is brutal well before this")
    clips = sorted(glob.glob(os.path.join(_CLIPS_ROOT, reel.name, "clip_*.mp4")))
    for c in clips:
        d = _duration(c)
        if 0 < d < 1.5:
            probs.append(f"{os.path.basename(c)} is only {d:.1f}s — too brief to register "
                         "(a truncated generation?)")
    return probs[:4]


CHECKS = {"playable": check_playable, "caption-safe-area": check_caption_safe_area,
          "realism": check_realism, "craft-layers": check_craft_layers,
          "style-uniform": check_style_uniform,
          "character-consistency": check_character_consistency, "pacing": check_pacing}


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
    dict(slug="craft-layers", check_id="craft-layers", severity="block",
         title="The reel SHIPS captions, in-scene narration, a moral card and a background score",
         why="the owner was taught these layers repeatedly and reels still went out without "
             "them; nothing machine-checked it, so it is now a blocking gate. Narration is "
             "spoken INSIDE the generated clips (WAVELENGTH.md §3), never a track added after"),
    dict(slug="style-uniform", check_id="style-uniform", severity="block",
         title="One art style for the WHOLE reel — every new reel is 3-D animation",
         why="per-frame review passes a reel whose scene 1 is photoreal and whose scene 2 is "
             "animated; WAVELENGTH.md §1 makes 3-D the house medium, so a reel that declares "
             "nothing and renders photoreal is caught here"),
    dict(slug="unique-reel-identity", tier="judgment", severity="block",
         title="A fresh identity every reel — opener, fonts, transitions, quote, structure",
         why="repeating an old reel's look is the flaw the owner caught before"),
    dict(slug="character-consistency", check_id="character-consistency", severity="block",
         title="The recurring character is the same person in every scene (checked in the FRAMES)",
         why="the shot prompts carry a character lock, but a lock in a prompt is not evidence "
             "about the render — a face can drift while every prompt names the same person"),
    dict(slug="pacing", check_id="pacing", severity="publish-gate",
         title="The reel's rhythm is usable — no shot too brief to register, no unfeedable length",
         why="a truncated generation or a 3-minute reel is broken whatever its content; mixed "
             "shot lengths and long hero holds are deliberate and are NOT flagged"),
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
         title="The bed is ducked UNDER the clips' own audio, never instead of it",
         why="the narration is spoken inside the generated clips (WAVELENGTH.md §3); a bed loud "
             "enough to mask it destroys the reel. Track choice is automated (music.py) and "
             "owner-approved after the fact — the owner's ear is the final check, not the gate"),
    dict(slug="music-license", tier="judgment", severity="publish-gate",
         title="Music provenance is RECORDED (not a gate)",
         why="owner instruction 2026-10-06, restated 2026-10-08: prioritise output quality, do "
             "not hold a reel back over licensing questions — but the source row stays in "
             "music/LIBRARY.md so provenance is never lost. Publication is the owner's call"),
]


def seed(st):
    for l in LESSONS:
        st.learn_lesson(source="memory-rules", **l)


# ---- a reel's artifacts, discovered from its project dir ------------------------------------
class Reel:
    # the runner/stitch path writes final.mp4; the manual craft path writes final_veo.mp4.
    # When BOTH exist the NEWEST one is the current cut — gating the stale one is a false pass.
    FINALS = ("final_veo.mp4", "final.mp4")

    def __init__(self, project_dir):
        self.dir = os.path.abspath(project_dir)
        self.name = os.path.basename(self.dir.rstrip("/"))
        found = [os.path.join(self.dir, f) for f in self.FINALS
                 if os.path.exists(os.path.join(self.dir, f))]
        self.final = max(found, key=os.path.getmtime) if found else None
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
                # a "could not run" is a CAPABILITY gap, not a defect: it never blocks the ship
                # (the reel may be perfect), it only gates go_live and asks a human to look.
                root = "publish-gate" if isinstance(p, Unreviewed) else l["severity"]
                bucket.get(root, warn).append(f"[{l['slug']}] {p}")
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

    verdict = {"ok": not block and not gate, "block": block, "gate": gate,
               "warn": warn, "checklist": checklist, "reel": reel.name}
    # persist the verdict: a HELD reel must be durable state anyone can read back
    # (brain.report / `brain.py reels`), not a printout that scrolls away.
    st.record_qc(reel.name, verdict)
    if own:
        st.close()
    return verdict


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

        # A "clean reel" fixture has to be a REAL reel length, or the pacing check fires on the
        # test itself and every passing fixture reads as HELD. The passing fixtures use _RL; the
        # deliberately-bad ones stay at 2s, which is also what proves pacing fires on a stub.
        _RL = 12

        # build a deliberately-bad reel: frozen silent video + a caption that sits in the UI zone
        d = tempfile.mkdtemp()
        # NOTE: this fixture deliberately has no VO/moral/score, so it also proves craft-layers
        # fires — a reel that plays but ships no voiceover is exactly the defect that shipped.
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
        # a real 1x1 transparent PNG: caption-safe-area OPENS the file, so a byte blob named
        # .png would raise instead of exercising the check we mean to prove
        import base64
        _PNG = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

        _orig_review = vision.review_frame
        _orig_medium = vision.medium_of
        _orig_cmp = vision.compare_character
        try:
            # the style classifier is a LIVE vision call too; stub it so --selfcheck stays offline
            vision.medium_of = lambda png: "3d"
            vision.review_frame = lambda png: None                # gateway unavailable
            # narration is read from the SOURCE CLIPS now (WAVELENGTH.md §3), not from a vo/
            # dir — so point the gate at a temp library holding one SILENT clip for this reel
            _CLIPS_ROOT = tempfile.mkdtemp()
            _cd = os.path.join(_CLIPS_ROOT, os.path.basename(d))
            os.makedirs(_cd)
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=gray:s=720x1280:d=1",
                            "-pix_fmt", "yuv420p", os.path.join(_cd, "clip_000.mp4")],
                           check=True, capture_output=True)
            v = run(d, st)
            assert any("frozen still" in b for b in v["block"]), v["block"]
            assert any("no audio" in b for b in v["block"]), v["block"]
            assert any("bottom-15%" in b for b in v["block"]), v["block"]
            assert any("source clips carry no audio" in b for b in v["block"]), \
                f"clips with no narration audio must be HELD: {v['block']}"
            assert any("no moral card" in b for b in v["block"]), v["block"]
            assert any("no background score" in b for b in v["block"]), v["block"]
            assert any("visual review could NOT run" in g for g in v["gate"]), \
                "a down vision gateway must say so, not silently pass"
            # pacing: this stub is a 2s reel holding a 1s source clip. Both are broken whatever
            # the content, so both are flagged — and neither is a taste judgement.
            assert any("[pacing]" in g and "too short" in g for g in v["gate"]), v["gate"]
            assert any("[pacing]" in g and "clip_000" in g for g in v["gate"]), v["gate"]
            assert not v["ok"], "a frozen silent reel with a bad caption must be HELD"

            vision.review_frame = lambda png: [{"type": "body-merge",
                                                "detail": "her torso overlaps his shoulder"}]
            v = run(d, st)
            assert any("body-merge" in g for g in v["gate"]), v["gate"]
            assert not v["ok"], "a reel with a vision-reported defect must be HELD"

            # a reel whose scene 1 came back photoreal and whose scene 3 is 3-D: every SINGLE
            # frame reviews clean, yet the reel mixes media. Only a cross-reel check catches it.
            vision.review_frame = lambda png: []
            d5 = tempfile.mkdtemp()
            os.makedirs(os.path.join(d5, "vo"))
            for _f in ("caption_hook.png", "caption_moral.png"):
                open(os.path.join(d5, _f), "wb").write(_PNG)
            open(os.path.join(d5, "music_raw.mp3"), "wb").write(b"x")
            open(os.path.join(d5, "vo", "hook.aiff"), "wb").write(b"x")
            json.dump({"opener": "lantern", "font": "Georgia", "quote": "Kabir", "structure": "arc"},
                      open(os.path.join(d5, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=2", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d5, "final_veo.mp4")],
                           check=True, capture_output=True)
            _seq = iter(["photoreal", "photoreal", "3d", "3d", "3d"])   # 2 photoreal -> drift
            vision.medium_of = lambda png: next(_seq, "3d")
            v5 = run(d5, st)
            assert any("not in ONE art style" in b for b in v5["block"]), \
                f"a photoreal-opener / 3-D-body reel must be HELD: {v5['block']}"
            vision.medium_of = lambda png: None
            v5b = run(d5, st)
            assert any("could NOT run" in g for g in v5b["gate"]), \
                "an unavailable classifier must gate go_live, not silently pass the reel"
            assert not any("could NOT run" in b for b in v5b["block"]), \
                f"an outage is not a style defect — it must not block the ship: {v5b['block']}"
            vision.medium_of = lambda png: "3d"
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
            os.makedirs(os.path.join(d3, "vo"))
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d3, _f), "wb").write(_PNG)
            open(os.path.join(d3, "music_raw.mp3"), "wb").write(b"x")
            open(os.path.join(d3, "vo", "hook.aiff"), "wb").write(b"x")
            json.dump({"opener": "typewriter", "font": "Snell", "quote": "Rumi", "structure": "montage"},
                      open(os.path.join(d3, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d3, "final_veo.mp4")],
                           check=True, capture_output=True)
            v3 = run(d3, st)
            assert v3["ok"] and not v3["block"], v3
            # A reel that declares NO medium inherits the house rule (WAVELENGTH.md §1: 3-D), so
            # the SAME reel judged photoreal is a defect — not because it disagrees with itself,
            # but because it is not what the house asked for.
            vision.medium_of = lambda png: "photoreal"
            v3p = run(d3, st)
            assert any("every new reel is 3-D animation" in b for b in v3p["block"]), \
                f"a reel that declared no medium and rendered photoreal must be HELD: {v3p['block']}"
            # ...but a reel that DECLARES photoreal is judged against its own declaration. Three
            # shipped reels here are photorealistic by design, and the house rule must never
            # retroactively declare them inconsistent with themselves and hold them.
            d6 = tempfile.mkdtemp()
            os.makedirs(os.path.join(d6, "vo"))
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d6, _f), "wb").write(_PNG)
            open(os.path.join(d6, "music_raw.mp3"), "wb").write(b"x")
            open(os.path.join(d6, "vo", "hook.aiff"), "wb").write(b"x")
            json.dump({"opener": "letters", "font": "Rozha One", "quote": "Kabir",
                       "structure": "bookend mirror", "visual_style": "photorealistic warm documentary"},
                      open(os.path.join(d6, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d6, "final_veo.mp4")],
                           check=True, capture_output=True)
            v6 = run(d6, st)
            assert not any("3-D animation" in b for b in v6["block"]), \
                f"a reel that DECLARES photoreal is judged against its own declaration: {v6['block']}"
            vision.medium_of = lambda png: "3d"

            # character consistency is the ONE cross-scene view of the render: one panel per
            # source clip, tiled, one question. Stub the gateway so this stays offline, and prove
            # all three outcomes — same person, a drift, and a down gateway (which must say so).
            d7 = tempfile.mkdtemp()
            _c7 = os.path.join(_CLIPS_ROOT, os.path.basename(d7))
            os.makedirs(_c7)
            for _i in range(3):
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=4",
                                "-f", "lavfi", "-i", "sine=frequency=440:d=4", "-pix_fmt", "yuv420p",
                                "-shortest", os.path.join(_c7, f"clip_{_i:03d}.mp4")],
                               check=True, capture_output=True)
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d7, _f), "wb").write(_PNG)
            open(os.path.join(d7, "music_raw.mp3"), "wb").write(b"x")
            json.dump({"opener": "close-up", "font": "Kalam", "quote": "Tulsidas",
                       "structure": "frame inside a frame"},
                      open(os.path.join(d7, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d7, "final_veo.mp4")],
                           check=True, capture_output=True)
            vision.compare_character = lambda png: {"consistent": True, "detail": ""}
            v7 = run(d7, st)
            assert v7["ok"] and not v7["block"], f"a consistent reel must pass: {v7}"
            vision.compare_character = lambda png: {
                "consistent": False, "detail": "her hair is grey in panel 1, black in panel 2"}
            v7b = run(d7, st)
            assert any("character changes between scenes" in b for b in v7b["block"]), v7b["block"]
            vision.compare_character = lambda png: None
            v7c = run(d7, st)
            assert any("could NOT be reviewed" in g for g in v7c["gate"]), \
                f"an unavailable comparison must gate go_live (not pass): {v7c['gate']}"
            assert not any("could NOT be reviewed" in b for b in v7c["block"]), \
                f"an OUTAGE is not a craft defect — it must not block the ship: {v7c['block']}"
            vision.compare_character = _orig_cmp

            # the verdict is DURABLE state, keyed by reel, and a re-run overwrites it
            # (v6, not v3: d3 is deliberately re-run as a defect just above, so its saved row
            #  is the HELD one — which is itself proof the upsert overwrites)
            assert st.qc_verdicts(v6["reel"])[0]["ok"] == 1, "a passing verdict is persisted"
            assert st.qc_verdicts(v3["reel"])[0]["ok"] == 0, "a re-run overwrites the saved verdict"
            assert st.qc_verdicts(v["reel"])[0]["ok"] == 0, "a HELD verdict is persisted too"
            # with both finals present the NEWEST cut is gated (stale file must be ignored)
            d4 = tempfile.mkdtemp()
            old, new = os.path.join(d4, "final_veo.mp4"), os.path.join(d4, "final.mp4")
            for p, when in ((old, 1000), (new, 9000)):
                open(p, "wb").write(b"x")
                os.utime(p, (when, when))
            assert Reel(d4).final == new, "newest final wins the gate"
            os.utime(old, (20000, 20000))
            assert Reel(d4).final == old, "the newer craft-path cut wins back"
        finally:
            vision.review_frame = _orig_review
            vision.medium_of = _orig_medium
            vision.compare_character = _orig_cmp
        st.close()
        print("OK: auto checks catch frozen/silent/off-safe-area + identity reuse; "
              "realism gates on a vision defect / reports a down gateway; clean reel passes")
        print("OK: verdict persisted to state.db; newest final*.mp4 is the one gated")
        print("OK: craft-layers holds a playable reel that ships no VO/moral/score")
        print("OK: pacing flags a stub reel and a 1s clip; character drift across scenes is HELD")
    elif "--seed" in sys.argv:
        st = State(); seed(st)
        print(f"seeded {len(st.craft_lessons())} craft lessons into state.db"); st.close()
    elif len(sys.argv) > 1:
        v = run(sys.argv[1])
        _report(v)
        sys.exit(0 if v["ok"] else 1)
    else:
        print(__doc__)
