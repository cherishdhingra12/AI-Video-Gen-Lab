"""
qc.py — the craft gate. state.py stores WHAT the owner taught (craft_lessons);
this runs the checks over a finished reel and returns a verdict before hand-off.

Two tiers, straight from the lesson rows:
  auto      a check_id here in CHECKS can look at the render and pass/fail it.
  judgment  no code can see it (style drift, emotional beats) -> a checklist for eyes.

    python3 qc.py <project_dir>      # gate one reel; exit 1 if anything blocks
    python3 qc.py <project_dir> --signed-by '<name>' [--note '...']
                                     # a human signs off the by-eye gaps on THIS cut, then re-gate
    python3 qc.py --seed            # (re)absorb the canonical lessons into state.db
    python3 qc.py --selfcheck       # offline: prove each check catches its failure

A blocking failure means DO NOT SHIP. A publish-gate means don't go_live yet (e.g.
music not license-cleared). The judgment checklist is for the human before approval.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.dirname(_DIR))   # project root, so the caption judge (agents.py) is
                                            # importable when qc runs standalone, not via runner.py
from state import State  # noqa: E402


# ---- auto checks: check_id -> fn(reel) -> list[problem strings] (empty = pass) ---------------

class Unreviewed(str):
    """An item a HUMAN must settle — not a defect, and not a pass.

    Two things land here. (a) A check that could NOT run: the three vision checks share the
    gateway that the QC frame-review exhausts, so an outage used to read exactly like a craft
    failure and HELD a clean, finished reel (darzi-ka-tukda, 2026-10-10: all vision routes
    rate-limited, every offline check green). (b) A JUDGE that ran and objected — the caption
    wording judge is a model's opinion of Hindi, and the owner's rule is that it must never stop a
    ship on its own, it asks for a human's eyes. Both mean the same thing to run(): the item gates
    go_live in the `eye` list, where a human's sign-off settles it, and neither ever blocks.

    Without (b) the gate could not be answered at all: the judge is non-deterministic, so
    "apply its fix, rebuild, re-judge" found a fresh stylistic objection every round — a ratchet
    with no end and no way for the owner to overrule it. Runs on 2026-10-11 gave three different
    verdicts on the same two cards.

    A str subclass so it prints and greps as the message."""


class Reject(str):
    """A DEFECT the machine can ACT on: the check ran, it found a real problem, and it knows
    WHICH generated clips caused it.

    This is the opposite of Unreviewed. Unreviewed says "nobody could answer this, ask a human";
    Reject says "the answer is bad and here is the clip to re-make". runner._regenerate reads
    `.clips`, requeues exactly those rows and appends `.reason` to their prompts.

    Why it has to exist: a finding the pipeline cannot act on is a finding the owner has to act
    on, and there was no path from "the gate says clip 3 is photoreal" back to a re-render. The
    one seam that could carry it (`state.requeue`) had no caller, so the only automated response
    to drift was `stitch_done_projects` retrying the SAME clips forever.

    A str subclass so it prints and greps as the message."""

    def __new__(cls, msg, clips=(), reason=None):
        o = super().__new__(cls, msg)
        o.clips = list(clips)          # source-clip idx values (the value state.requeue takes)
        o.reason = reason or str(msg)  # what to append to the re-shot clip's prompt
        return o

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


def check_no_synth_voiceover(reel):
    """WAVELENGTH.md §3: every spoken line is generated INSIDE the clip. A `vo/` folder of
    separately synthesized lines (macOS `say`, edge-tts) is the thing the owner banned — three
    shipped reels (`aawaaz`, `filter_wala`, `the-lamp-s-silent-promise`) still carry one.

    A publish-gate, never a block: an existing finished reel stays shippable, but it does not
    go_live until a human has heard whether a synthetic voice is riding over the clip's own.
    Returned as a plain string -> the gate routes it by the lesson's severity (publish-gate)."""
    vo = glob.glob(os.path.join(reel.dir, "vo", "*"))
    if vo:
        return [(f"a separately synthesized voiceover is present (vo/{os.path.basename(vo[0])}, "
                 f"{len(vo)} file(s)) — narration must be spoken inside the generated clip")]
    return []


_PHOTOREAL_WORDS = ("photoreal", "live action", "documentary", "real footage")

# Reels approved BEFORE the 2026-10-08 house rule, photoreal by the owner's own design. They are
# grandfathered so the rule never retroactively holds an approved deliverable — and this list is
# CLOSED: no reel shipped after it may join. (See WAVELENGTH.md §1.)
PHOTOREAL_GRANDFATHERED = {"apne", "haath_thaame", "roz_ki_list"}


def _declared_medium(reel) -> str:
    """The medium a reel is JUDGED against — the house 3-D rule (WAVELENGTH.md §1) for every reel,
    whatever its identity.json claims. A photoreal declaration counts ONLY for a grandfathered
    pre-2026-10-08 reel.

    Before this, ANY reel that wrote 'photorealistic' into identity.json was then checked against
    photoreal and passed itself — which is exactly how `dadaji-ka-kahani` (2026-10-10) shipped a
    photoreal reel past a 3-D house rule: the generator drifted and its identity was rewritten to
    DESCRIBE the drift, and the description disarmed the gate. A declaration is a description, not
    a permission; the owner's rule (2026-10-10) is that every Standard Sunrise reel is 3-D."""
    if reel.name in PHOTOREAL_GRANDFATHERED:
        s = ((reel.identity or {}).get("visual_style") or "").lower()
        return "photoreal" if any(k in s for k in _PHOTOREAL_WORDS) else _HOUSE_MEDIUM
    return _HOUSE_MEDIUM


def _clip_paths(reel, limit=6):
    """The reel's SOURCE clips, in reel order. Source clips, not the final cut: no caption card is
    overlaid, so a face and a render can be compared as generated. Empty once a published reel
    has been swept — nothing to inspect."""
    return sorted(glob.glob(os.path.join(_CLIPS_ROOT, reel.name, "clip_*.mp4")))[:limit]


def _clip_frames(reel, frac=0.5, limit=6):
    """One frame from `frac` through each source clip, PNG bytes, same order as _clip_paths."""
    out = []
    for c in _clip_paths(reel, limit):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{_duration(c) * frac:.3f}", "-i", c,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if r.stdout:
            out.append(r.stdout)
    return out


def _idx_of(path):
    """The clip's idx — the number in `clip_003.mp4`, which is exactly what state.requeue takes.
    Read off the filename, never the list position: a gap on disk would shift positions and the
    requeue would re-shoot the wrong clip."""
    m = re.search(r"clip_(\d+)\.mp4$", path)
    return int(m.group(1)) if m else None


def check_style_uniform(reel):
    """One art style across the WHOLE reel. review_frame catches a frame that breaks its OWN
    look (limbs, merges, text), but a reel can be frame-perfect photoreal in scene 1 and
    animated from scene 2 — every individual frame is 'fine', the reel is inconsistent. That is
    the defect the owner actually reported, and it needs a check that compares ACROSS the reel.

    TWO questions, asked separately because they fail separately:
      (1) MEDIUM — is each frame the look the house asks for? Classified per frame, judged
          against _declared_medium.
      (2) AGREEMENT — do the clips match EACH OTHER? Asked pairwise against clip 0, the first
          clip finished, which is what locked the reel's site and character.

    (2) is not optional and not the same question as (1). Classifying a frame into four buckets
    collapses a whole photoreal clip into '3d' when its neighbours are 3-D — which is how
    dadi-ki-dhansuchi (2026-10-12) passed this check carrying a photoreal scene. Pairwise
    comparison answers with a reason AND a clip index, so the machine can re-shoot it.
    """
    if not reel.final:
        return []
    import vision
    probs = []
    d = _duration(reel.final)
    if d > 0:
        samples = []
        for frac in (0.06, 0.30, 0.55, 0.80, 0.95):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{d * frac:.3f}", "-i", reel.final,
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True)
            if r.stdout:
                samples.append(vision.medium_of(r.stdout))
        # Judged against the HOUSE medium (3-D) unless this is a grandfathered pre-2026-10-08 reel
        # — see _declared_medium. A new reel's own photoreal declaration no longer loosens this.
        verdict, ev = vision.medium_verdict(samples, expected=_declared_medium(reel))
        if verdict == "unknown":
            probs.append(Unreviewed("style classification could NOT run (vision gateway "
                                    "unavailable) — confirm by eye the reel stays in ONE art style"))
        elif verdict == "drift":
            probs.append(f"the reel is not in ONE art style ({ev}) — no photoreal scenes in front "
                         "of animated ones")
        elif verdict == "wrong-medium":
            probs.append(f"{ev} — WAVELENGTH.md §1: every new reel is 3-D animation")

    paths = _clip_paths(reel)
    if len(paths) >= 2:
        scan = vision.style_scan_cached(_clip_frames(reel))
        if scan is None:
            probs.append(Unreviewed("style drift could NOT be reviewed clip-against-clip (vision "
                                    "gateway unavailable) — confirm by eye every scene is one look"))
        elif scan["whole_reel"]:
            # The anchor is the odd one out, so "re-shoot the rest" would be re-shooting the reel.
            # That is the owner's call, never a requeue — and it is what protects a genuinely
            # photoreal reel from being auto-re-made into something else.
            probs.append(Unreviewed(
                f"every clip differs from the reel's first clip ({scan['detail']}) — the whole "
                "reel is one look, so this is not one bad clip: confirm by eye before re-shooting"))
        elif scan["flagged"]:
            idxs = [i for i in (_idx_of(paths[i]) for i in scan["flagged"]) if i is not None]
            names = ", ".join(f"clip {i}" for i in idxs) or "a clip"
            probs.append(Reject(
                f"{names} in a different look from the rest of the reel ({scan['detail']})",
                clips=idxs,
                reason=f"the previous render of this clip did not match the reel's look "
                       f"({scan['detail']}): hold the SAME art style and the SAME level of "
                       f"realism as every other shot in this film"))
    return probs


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

    Read twice, from two directions, because a single answer to 'same person?' is a coin flip on
    the frames this house actually shoots: the face is small, the render is soft, and a bindi is
    not a different woman (the by-eye pass of 2026-10-12 convicted exactly that, wrongly, before
    the measurement corrected it). So:
      * the SHEET — every panel at once, "one person throughout?" (vision.compare_character)
      * the PAIRWISE scan — each clip against clip 0, the same call that answers the look
    A defect needs BOTH to agree. One voice saying drift, the other saying fine, is not a
    conviction — it is a question for a human, and unreviewed is where it goes. That direction
    matters: cross-checking can only ADD doubt, never clear a defect both saw (WAVELENGTH §6 —
    never re-roll a judge until it agrees).

    Reads the SOURCE clips (no caption cards overlaid to confuse the comparison), and follows
    craft-layers' convention: clips swept after publish means there is nothing to inspect."""
    if not reel.final:
        return []
    frames = _clip_frames(reel)
    if len(frames) < 2:
        return []                                   # one panel cannot disagree with itself
    import vision
    sheet = _contact_sheet(frames)
    res = vision.compare_character(sheet) if sheet is not None else None
    scan = vision.style_scan_cached(frames)
    pairs_drift = [i for i, same in (scan or {}).get("people", {}).items() if not same]
    scan_ok = scan is not None
    if res is None and not scan_ok:
        return [Unreviewed("character consistency could NOT be reviewed (vision gateway "
                           "unavailable) — check by eye that the same face and hair appear in "
                           "every scene")]
    if res is not None and not res["consistent"] and pairs_drift:
        idxs = [i for i in (_idx_of(_clip_paths(reel)[i]) for i in pairs_drift) if i is not None]
        return [Reject(
            "the recurring character changes between scenes: "
            f"{res['detail'] or scan.get('person_detail') or 'the panels do not show the same person'}",
            clips=idxs,
            reason=f"the character is not the same person as in the rest of the film "
                   f"({res['detail'] or scan.get('person_detail')}): hold the SAME face shape, "
                   f"age, hair and features as the first clip, and no jewellery or make-up the "
                   f"character does not wear there")]
    if (res is not None and not res["consistent"]) or pairs_drift:
        who = "the panel review" if (res is not None and not res["consistent"]) else \
              "the clip-against-clip scan"
        return [Unreviewed(
            f"{who} says the recurring character changes between scenes and the other view does "
            "not — the two do not agree, so this is not a conviction. Look at the frames and "
            f"settle it ({res['detail'] if res is not None and not res['consistent'] else scan.get('person_detail', '')})")]
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


def _window_db(path, ss, dur):
    """Mean level (dBFS) of one window of a reel's audio — ffmpeg's own volumedetect, so this
    agrees with the measure the assembler records."""
    for ln in _run(["ffmpeg", "-hide_banner", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}",
                    "-i", path, "-af", "volumedetect", "-f", "null", "-"]).stderr.splitlines():
        if "mean_volume:" in ln:
            try:
                return float(ln.split("mean_volume:")[1].split("dB")[0])
            except (ValueError, IndexError):
                return None
    return None


def _audio_floor(path, step=1.0):
    """The quietest 1s window's mean level — the reel's audio floor. A score carrying the reel
    keeps this within ~10 dB of the narration; a bed that is absent, or that played its own slow
    intro through the hook, drops the floor far below anything the reel says. Measured across the
    whole fleet (2026-10-10): every healthy reel floors at -29..-41 dB, darzi-ka-tukda
    (silent-intro bed) at -50.2, roz_ki_list (the same defect) at -48.3, a no-bed rebuild at
    -56.3. The gap is ~7 dB wide, so the floor is a robust defect signal — unlike silencedetect,
    which saw darzi's -45.2 dB intro as 'not silent' and passed it."""
    d = _duration(path)
    if not d:
        return None
    vals, t = [], 1.0
    while t < d - 1.2:
        v = _window_db(path, t, step)
        if v is not None:
            vals.append(v)
        t += step
    return round(min(vals), 2) if vals else None


FLOOR_BAD = -46.0            # dB; below this the reel's audio has a hole no narration explains


def check_score_present(reel):
    """The background score must be AUDIBLE IN the rendered audio, not merely on the shelf.
    craft-layers only checks that projects/<reel>/music_raw.mp3 exists — which passes a reel whose
    bed was mixed in but silent through its hook (the track opened on a slow alap) or one whose
    build skipped the mix entirely. Neither a file's existence nor a silencedetect scan catches
    that; the audio floor does (see _audio_floor for the fleet measurement behind the threshold).
    A quiet beat between lines is a performance choice — only a floor this low is a defect."""
    if not reel.final or not glob.glob(os.path.join(reel.dir, "music_raw.mp3")):
        return []                                   # craft-layers owns 'no score at all'
    d = _duration(reel.final)
    if d < 6:
        return []
    floor = None
    meta = os.path.join(reel.dir, "music_bed.json")
    if os.path.exists(meta):
        try:
            floor = json.load(open(meta)).get("floor")
        except (OSError, ValueError, TypeError):
            floor = None
    if floor is None:                               # older build: no sidecar floor — measure now
        floor = _audio_floor(reel.final)
    if floor is None:
        return [Unreviewed(f"could not measure the audio floor of {os.path.basename(reel.final)} "
                           "— the score's presence in the mix is unproven")]
    if floor < FLOOR_BAD:
        return [f"the background score does not carry the reel — its quietest stretch sits at "
                f"{floor:.1f} dBFS (a reel with a bed throughout floors near -35). The bed may be "
                f"missing, or the track's quiet intro played through the hook. Re-mix with the "
                f"intro skipped."]
    return []


def check_caption_on_face(reel):
    """A caption card must never sit on a face. This is the defect the owner caught: the opening
    title printed straight across the grandfather's face, because placement scored 'skin' by
    COLOUR on a warm amber frame — the wall read as skin, the mask became noise, and the softest
    patch (the face) won. Placement now finds faces with macOS Vision (caption.py), and this reads
    the final cut the same way, over exactly the window each card occupies (recorded by the
    assembler in caption_place.json, so a card is never matched against a face from another
    scene). Offline, deterministic, no vision gateway.

    Best-effort: a face detector can miss an unusual framing, so a clean result is 'the detector
    found no card over any face', not a guarantee. It only ever raises a flag."""
    if not reel.final:
        return []
    p = os.path.join(reel.dir, "caption_place.json")
    if not os.path.exists(p):
        return []                                     # a build from before windows were recorded
    try:
        import caption
        place = json.load(open(p, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    probs = []
    for kind, w in sorted(place.items()):
        frames = []
        for i in range(5):
            t = w["start"] + (w["end"] - w["start"]) * (i + 0.5) / 5
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", reel.final,
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True)
            if r.stdout:
                frames.append(r.stdout)
        faces = [f for fr in caption.faces(frames) for f in fr]
        hits = [f for f in faces
                if not (f[1] + f[3] < w["y0"] or f[1] > w["y1"])
                and not (f[0] + f[2] < w["x0"] or f[0] > w["x1"])]
        if hits:
            probs.append(f"caption_{kind}.png: a face is inside the card (card y{w['y0']}-"
                         f"{w['y1']}, face at y{hits[0][1]:.0f}) — it must sit in an empty band")
    return probs


def check_caption_wording(reel):
    """Do the caption CARDS say the right thing, in correct Hindi? — layer 2 of the caption gate.

    Every other caption check is geometry (safe area, never on a face) or existence (craft
    layers). Nothing read the WORDS, so dadi-ki-dhansuchi shipped "उनकी मुस्कान बनें ख़ज़ाना" — a
    verbless fragment whose बनें is the respectful plural bolted onto a feminine singular — and
    ख़ज़ाना spelled two ways in one reel, and no check so much as blinked.

    The words are a JUDGEMENT, not a rule: the cards go to agents.judge_captions with the script,
    and what comes back is what a Hindi reader would wince at. Layer 1 (the art director reading
    its own cards back at kickoff) has already repaired them, so this is the independent second
    look on the FROZEN artifacts — after the render, on exactly what will ship.

    A down gateway says so (Unreviewed -> publish-gate) rather than passing. It never BLOCKS: a
    model's opinion of Hindi must not stop a ship on its own, it asks for a human's eyes — and
    like every other human item it is settled by the sign-off (`--signed-by`), not by re-rolling
    the model until it happens to agree."""
    if not reel.final:
        return []
    caps_p = os.path.join(reel.dir, "captions.json")
    if not os.path.exists(caps_p):
        return []                                     # no cards on disk -> craft-layers HELDs it
    try:
        import agents
        caps = json.load(open(caps_p, encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable cards are craft-layers' business, not this one
        return []
    sp = os.path.join(reel.dir, "script.md")
    script = open(sp, encoding="utf-8").read() if os.path.exists(sp) else ""
    try:
        out = agents.judge_captions(script, caps)
    except Exception:  # noqa: BLE001 — no gateway is an OUTAGE, never a pass and never a defect
        return [Unreviewed("caption wording could NOT be judged (LLM gateway unavailable) — "
                           "read the cards in Hindi before go_live")]
    return [Unreviewed(f"caption wording: {p}") for p in out.get("problems") or []]


CHECKS = {"playable": check_playable, "caption-safe-area": check_caption_safe_area,
          "caption-on-face": check_caption_on_face, "caption-wording": check_caption_wording,
          "realism": check_realism, "craft-layers": check_craft_layers,
          "no-synth-voiceover": check_no_synth_voiceover,
          "style-uniform": check_style_uniform,
          "character-consistency": check_character_consistency, "pacing": check_pacing,
          "score-present": check_score_present}


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
    dict(slug="caption-never-on-a-face", check_id="caption-on-face", severity="block",
         title="A caption card NEVER overlaps a face — it sits in the frame's empty band",
         why="the owner caught the opening title printed straight across the grandfather's face. "
             "It happened because placement scored 'skin' by COLOUR and a warm amber room made "
             "the wall read as skin, so the mask was noise and the softest patch won. Placement "
             "now detects faces with macOS Vision (caption.py) and this check re-answers 'is a "
             "face under this card?' against the shipped cut"),
    dict(slug="caption-wording", check_id="caption-wording", severity="publish-gate",
         title="The caption cards say the RIGHT thing, in correct Hindi",
         why="dadi-ki-dhansuchi (2026-10-10) shipped a hook that read 'उनकी मुस्कान बनें ख़ज़ाना' — "
             "a verbless fragment whose बनें is the respectful plural bolted onto a feminine "
             "singular — and spelled ख़ज़ाना two ways in one reel. Nothing caught it because every "
             "caption check was geometry or existence; no check read the WORDS. The hook was also "
             "not a quote but a translation (the script wrote it in English), so the model invented "
             "the Devanagari. Two layers now: agents.presentation has the art director read its own "
             "cards back before writing them, and this independently judges the frozen artifacts "
             "against the script after the render — grammar, register, spelling consistency, and "
             "whether each card still says the script's beat. A judgement, never a rule, and a "
             "publish-gate: it asks for a human's eyes rather than stopping the ship"),
    dict(slug="no-text-in-the-film", tier="judgment", severity="warn",
         title="The generated film carries NO text of its own — every card is added after the cut",
         why="a prompt that asks for a caption, a heading or a screen with words gets gibberish "
             "Devanagari baked into the frame, and the real card then reads as a fake pasted over "
             "nonsense — the owner caught it in the closing shot. The generator cannot be trusted "
             "with text, so agents.clip_prompts attaches the no-text rule to EVERY prompt by "
             "code, and this is the by-eye backstop"),
    dict(slug="camera-never-into-frame", tier="judgment", severity="warn",
         title="Camera moves never travel INTO a frame, photo, screen or object",
         why="the owner cut exactly such a move out of a reel (the camera pushing INTO a wall "
             "photo). A generator obeys it literally and the shot becomes a lurching zoom. Only "
             "four moves are allowed — slow push-in, slow pull-back, pan, drift; a memory shown "
             "inside a photo is reached by cutting to it or panning ACROSS it (WAVELENGTH §5)"),
    dict(slug="gaps-between-lines", tier="judgment", severity="warn",
         title="Each clip is ONE span around its line — late start, early end, never a splice",
         why="assembled clips pad the line with nothing on both sides, so a 10s clip can carry 5s "
             "of speech. assemble._keep_windows takes ONE span per clip — PRE (0.5s) before the "
             "first line, POST (0.6s) after the last — and cuts nothing INSIDE it, however silent "
             "it measures: a pause between two lines is a performance beat, and on the reel that "
             "taught this, one 'silent' hole is three seconds of the old woman smiling at camera. "
             "Cutting the middle seams the picture, which is what the owner saw as 'very abrupt "
             "back-to-back cuts' (nine splices across one reel). A clip with no voice at all is "
             "never cut by the sound: it is left whole and reported, for a human's eye to settle"),
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
    dict(slug="score-present", check_id="score-present", severity="block",
         title="The background score actually carries the reel (audible in the FINAL, not just on disk)",
         why="a music_raw.mp3 on the shelf is not an audible score: a bed that opens on the "
             "track's slow intro is silent through the hook (darzi-ka-tukda, 2026-10-10), and an "
             "earlier build can skip the mix entirely — the owner hears the gap even when the "
             "file exists"),
    dict(slug="style-uniform", check_id="style-uniform", severity="block",
         title="Every Standard Sunrise reel is 3-D animation — never photoreal",
         why="WAVELENGTH.md §1: 3-D is the house medium, and writing 'photorealistic' into "
             "identity.json is NOT a hatch — a declaration describes, it does not permit. "
             "dadaji-ka-kahani (2026-10-10) shipped photoreal when the generator drifted and its "
             "identity was then rewritten to describe the drift, which disarmed this gate; the "
             "same check also catches a reel that mixes photoreal and animated scenes. Only the "
             "three pre-2026-10-08 reels (apne, haath_thaame, roz_ki_list) are grandfathered "
             "(qc.PHOTOREAL_GRANDFATHERED — a CLOSED list)"),
    dict(slug="medium-stated-first", tier="judgment", severity="warn",
         title="State the 3-D requirement at the START — script, shot list, and the FIRST line of "
               "every clip prompt — not after the render",
         why="owner rule 2026-10-10: catch the medium before generation, don't correct it after. A "
             "prompt that only describes the SCENE lets the model choose photoreal (dadaji-ka-"
             "kahani). agents.write_script and build_shotlist carry the requirement (and repair a "
             "non-animated style to HOUSE_STYLE), and agents.clip_prompts LEADS every prompt with "
             "'REQUIRED OUTPUT: 3-D ANIMATED VIDEO' then forces the house anchor; the gate "
             "(style-uniform) is the LAST resort, never the first line of defence"),
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
    dict(slug="no-synth-voiceover", check_id="no-synth-voiceover", severity="publish-gate",
         title="No separately synthesized voiceover — every line is spoken INSIDE the clip",
         why="WAVELENGTH.md §3: the clip's own voice carries the narration, so lip and line "
             "agree. A `vo/` folder of `say`/TTS lines mixed at assembly is banned — the reel "
             "may ship, but a human must hear it before go_live (three legacy reels carry one)"),
    dict(slug="veo-narration-not-lipsync", tier="judgment", severity="publish-gate",
         title="Narration is spoken IN the generated scene, never a muxed VO track",
         why="WAVELENGTH.md §3 bans after-the-fact synthesis; the line is written into the "
             "generation prompt so a character on camera speaks it (agents.clip_prompts), and a "
             "voice laid over the top is exactly the defect this rule exists to stop"),
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
    """Push the code's canonical craft lessons into the brain. Upserts by slug, so it is also how
    a lesson ADDED TO THIS FILE LATER reaches a brain that already exists — running it only when
    the brain was empty (the old behaviour) meant 'caption-never-on-a-face' and 'no-text-in-the-
    film', taught to the code this session, never landed in state.db."""
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

    @property
    def fp(self):
        """This CUT's fingerprint. A by-eye sign-off is a signature on a picture, so it is tied to
        the file: rebuild the reel and the signature no longer covers what is there."""
        if not self.final:
            return ""
        s = os.stat(self.final)
        return f"{int(s.st_mtime)}:{s.st_size}"


def _eye_artifacts(reel, items):
    """Write the look a human owes this reel (WAVELENGTH §6): frames sampled across the WHOLE cut
    plus ONE contact sheet — 'same person in every panel?' is a question no single frame answers.

    It goes on disk next to the reel because the reviewer is a person with a file browser, not a
    terminal: `eye_review.json` names the sheet and every item that needs a human's eyes.
    """
    d = _duration(reel.final)
    n = 6
    frames = []
    for i in range(n):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{d * (i + 0.5) / n:.3f}",
                            "-i", reel.final, "-frames:v", "1", "-f", "image2pipe",
                            "-vcodec", "png", "-"], capture_output=True)
        if r.stdout:
            frames.append(r.stdout)
    sheet = _contact_sheet(frames) if frames else None
    out = {}
    if sheet:
        p = os.path.join(reel.dir, "eye_review_sheet.png")
        with open(p, "wb") as fh:
            fh.write(sheet)
        out["sheet"] = os.path.basename(p)
    # a sampled frame in full size too: a 300px panel cannot show a caption sitting on a face
    for i, png in enumerate(frames[:2]):
        p = os.path.join(reel.dir, f"eye_review_{i + 1}.png")
        with open(p, "wb") as fh:
            fh.write(png)
        out[f"frame{i + 1}"] = os.path.basename(p)
    out |= {"reel": reel.name, "cut": os.path.basename(reel.final), "fp": reel.fp,
            "could_not_run": items, "at": time.time(),
            "sign_off": f"python3 automation/qc.py {reel.dir} --signed-by <your name>"}
    with open(os.path.join(reel.dir, "eye_review.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return out


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
    seed(st)                     # upsert, every run: a lesson added to LESSONS lands here too
    reel = Reel(project_dir)
    block, gate, warn, checklist = [], [], [], []
    eye = []                       # what a HUMAN must settle — not a defect, not a pass
    reject = []                    # what the MACHINE can re-make — clip indices + the reason
    bucket = {"block": block, "publish-gate": gate, "warn": warn}
    import vision
    _answered = len(vision.ANSWERED)      # everything the checks ask is appended here (see vision)

    for l in st.craft_lessons():
        if l["tier"] == "auto" and l["check_id"] in CHECKS:
            for p in CHECKS[l["check_id"]](reel):
                # a human item is a CAPABILITY gap or a judge's OPINION, not a defect: it never
                # blocks the ship (the reel may be perfect), it gates go_live and asks for eyes.
                root = "publish-gate" if isinstance(p, Unreviewed) else l["severity"]
                line = f"[{l['slug']}] {p}"
                bucket.get(root, warn).append(line)
                if isinstance(p, Unreviewed):
                    eye.append(line)
                elif isinstance(p, Reject) and p.clips:
                    reject.append({"check": l["slug"], "clips": p.clips, "reason": p.reason})
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
               "warn": warn, "checklist": checklist, "reel": reel.name, "eye": eye,
               "reject": reject,
               # WHO answered the vision questions this verdict rests on. Without it a verdict is
               # unattributable: 'clip 2 does not match' is an argument you can only have with a
               # named model, and a failover silently changing that model is exactly what nobody
               # would notice. Distinct models, in the order they first answered.
               "vision": sorted({a["model"] for a in vision.ANSWERED[_answered:]})}
    # `ok` is the AUTOMATED gate. `live` is the one that matters: a reel whose only gate items are
    # checks that could not run may ship, but it does not go_live until a human has looked at THIS
    # cut (WAVELENGTH §6). A reel with a real defect never clears, whoever signs.
    signed = st.eye_review(reel.name)
    verdict["eye_signed"] = bool(signed and signed["fp"] == reel.fp)
    verdict["live"] = bool(verdict["ok"] or (eye and not block and len(gate) == len(eye)
                                             and verdict["eye_signed"]))
    # persist the verdict: a HELD reel must be durable state anyone can read back
    # (brain.report / `brain.py reels`), not a printout that scrolls away.
    st.record_qc(reel.name, verdict)
    if eye:
        _eye_artifacts(reel, eye)
    if own:
        st.close()
    return verdict


def _report(v):
    print(f"QC: {v['reel']}")
    if v.get("vision"):
        print(f"  judged by: {', '.join(v['vision'])}")
    for tag, items in (("BLOCK — do not ship", v["block"]),
                       ("PUBLISH-GATE — don't go_live yet", v["gate"]),
                       ("warn", v["warn"])):
        for i in items:
            print(f"  {tag}: {i}")
    if v["checklist"]:
        print("  judgment checklist (human eyes before approval):")
        for sev, line in v["checklist"]:
            print(f"    - ({sev}) {line}")
    if v.get("reject"):
        print("  machine-fixable — these clips can be re-shot with the reason attached:")
        for r in v["reject"]:
            print(f"    - clip {', '.join(str(i) for i in r['clips'])} ({r['check']}): "
                  f"{r['reason']}")
    if v.get("eye"):
        print("  needs YOUR eyes before go_live (a check could not run, or a judge objects — "
              "neither is a defect, neither is a pass):")
        for i in v["eye"]:
            print(f"    - {i}")
        print("    -> the sheet and frames are in the project dir (eye_review.json)")
        print("    a human looks, then:  python3 automation/qc.py projects/<name> "
              "--signed-by '<name>'")
    if v["ok"]:
        print("  => PASS (auto checks clean; review the checklist)")
    elif v.get("live"):
        print("  => LIVE — every gap was a check that could not run, and a human has signed "
              "off THIS cut")
    elif v.get("eye") and not v["block"] and len(v["gate"]) == len(v["eye"]):
        print("  => SHIPPABLE, NOT LIVE — no human has signed off this cut yet")
    else:
        print("  => HELD (fix the block/gate items above)")


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

        def _scan(ans, model=None):
            """Point the pairwise scan at a FIXED answer, and drop its cache.

            The cache is keyed by frame bytes (so the two checks comparing the same reel compare
            the same ANSWER, not two rolls of the same question — see vision.style_scan_cached),
            and these fixtures all reuse one temp reel's unchanged frames. Without the clear,
            every fixture after the first would be answered by the first fixture's stub.

            `model` fakes the attribution a real gateway reports (vision.ANSWERED)."""
            if model:
                vision.style_scan = lambda frames: (vision.ANSWERED.append(
                    {"url": "stub", "asked": model, "model": model}), ans)[1]
            else:
                vision.style_scan = lambda frames: ans
            vision._SCAN_CACHE.clear()
        # a real 1x1 transparent PNG: caption-safe-area OPENS the file, so a byte blob named
        # .png would raise instead of exercising the check we mean to prove
        import base64
        _PNG = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

        _orig_review = vision.review_frame
        _orig_medium = vision.medium_of
        _orig_cmp = vision.compare_character
        _orig_scan = vision.style_scan
        try:
            # the style classifier is a LIVE vision call too; stub it so --selfcheck stays offline
            vision.medium_of = lambda png: "3d"
            # the pairwise scan is a LIVE call as well, and one per clip — an unstubbed run sits
            # on the network for a minute per fixture. Every fixture below that does not mean to
            # test drift gets the clean answer; the drift fixtures override this one line.
            _scan({"flagged": [], "whole_reel": False, "detail": "", "people": {},
                   "person_detail": ""})
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
            for _f in ("caption_hook.png", "caption_moral.png"):
                open(os.path.join(d5, _f), "wb").write(_PNG)
            open(os.path.join(d5, "music_raw.mp3"), "wb").write(b"x")
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
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d3, _f), "wb").write(_PNG)
            open(os.path.join(d3, "music_raw.mp3"), "wb").write(b"x")
            json.dump({"opener": "typewriter", "font": "Snell", "quote": "Rumi", "structure": "montage"},
                      open(os.path.join(d3, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d3, "final_veo.mp4")],
                           check=True, capture_output=True)
            v3 = run(d3, st)
            assert v3["ok"] and not v3["block"], v3
            # ...but drop a synthesized `vo/` into it and the reel is gated (never blocked):
            # WAVELENGTH.md §3 — narration is spoken in the clip, not muxed at assembly.
            os.makedirs(os.path.join(d3, "vo"))
            open(os.path.join(d3, "vo", "hook.aiff"), "wb").write(b"x")
            v3v = run(d3, st)
            assert any("synthesized voiceover" in g for g in v3v["gate"]), \
                f"a reel carrying a TTS vo/ folder must gate go_live: {v3v}"
            assert not any("synthesized voiceover" in b for b in v3v["block"]), \
                f"a synthesized VO gates, it does not block the ship: {v3v['block']}"
            shutil.rmtree(os.path.join(d3, "vo"))
            assert run(d3, st)["ok"], "removing the vo/ folder clears the gate again"
            # A reel that declares NO medium inherits the house rule (WAVELENGTH.md §1: 3-D), so
            # the SAME reel judged photoreal is a defect — not because it disagrees with itself,
            # but because it is not what the house asked for.
            vision.medium_of = lambda png: "photoreal"
            v3p = run(d3, st)
            assert any("every new reel is 3-D animation" in b for b in v3p["block"]), \
                f"a reel that declared no medium and rendered photoreal must be HELD: {v3p['block']}"
            # A reel that DECLARES photoreal but is NOT grandfathered is still held to the house
            # 3-D rule — the declaration is a description, not a hatch a new reel can open. This is
            # the dadaji-ka-kahani case: a drifted photoreal reel whose identity was rewritten to
            # say so must be HELD, not passed.
            d6 = tempfile.mkdtemp()
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d6, _f), "wb").write(_PNG)
            open(os.path.join(d6, "music_raw.mp3"), "wb").write(b"x")
            json.dump({"opener": "letters", "font": "Rozha One", "quote": "Kabir",
                       "structure": "bookend mirror", "visual_style": "photorealistic warm documentary"},
                      open(os.path.join(d6, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d6, "final_veo.mp4")],
                           check=True, capture_output=True)
            v6 = run(d6, st)
            assert any("3-D animation" in b for b in v6["block"]), \
                f"a NON-grandfathered reel declaring photoreal must still be HELD: {v6['block']}"
            # ...but a GRANDFATHERED reel (approved photoreal before the house rule) is judged
            # against its own declaration, so the rule never retroactively holds it. Keyed on the
            # reel's folder name (qc.PHOTOREAL_GRANDFATHERED).
            g6 = os.path.join(tempfile.mkdtemp(), "apne")
            os.makedirs(g6)
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(g6, _f), "wb").write(_PNG)
            open(os.path.join(g6, "music_raw.mp3"), "wb").write(b"x")
            json.dump({"opener": "shadow-play on a wall", "font": "Tiro Devanagari Hindi",
                       "quote": "Surdas", "structure": "a circle closes",
                       "visual_style": "photorealistic warm documentary"},
                      open(os.path.join(g6, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(g6, "final_veo.mp4")],
                           check=True, capture_output=True)
            v6g = run(g6, st)
            assert not any("3-D animation" in b for b in v6g["block"]), \
                f"a grandfathered photoreal reel must not be held: {v6g['block']}"
            vision.medium_of = lambda png: "3d"

            # character consistency is the ONE cross-scene view of the render: one panel per
            # source clip, tiled, one question — asked TWICE, from two directions. A defect needs
            # both voices; one saying drift while the other says fine is a question for a person,
            # not a conviction (the by-eye pass of 2026-10-12 convicted 'a different woman'
            # wrongly — a bindi is not a different woman — so character identity gets a second
            # opinion before it costs a re-render).
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
            # one voice says drift, the other says fine -> NOT a conviction: it asks for eyes
            vision.compare_character = lambda png: {
                "consistent": False, "detail": "her hair is grey in panel 1, black in panel 2"}
            v7x = run(d7, st)
            assert not v7x["block"], f"one dissenting view must not block: {v7x['block']}"
            assert any("do not agree" in g for g in v7x["gate"]), v7x["gate"]
            # BOTH views agree -> a real defect, and it names the clips to re-shoot
            _scan({"flagged": [], "whole_reel": False, "detail": "",
                   "people": {1: False, 2: False}, "person_detail": "the bindi is gone"})
            v7b = run(d7, st)
            assert any("character changes between scenes" in b for b in v7b["block"]), v7b["block"]
            assert v7b["reject"] and v7b["reject"][0]["clips"] == [1, 2], \
                f"a confirmed character drift must name the clips to re-shoot: {v7b['reject']}"
            assert "SAME face shape" in v7b["reject"][0]["reason"], \
                "the re-shot clip's prompt must carry the character lock"
            vision.compare_character = lambda png: None
            _scan(None)
            v7c = run(d7, st)
            assert any("could NOT be reviewed" in g for g in v7c["gate"]), \
                f"an unavailable comparison must gate go_live (not pass): {v7c['gate']}"
            assert not any("could NOT be reviewed" in b for b in v7c["block"]), \
                f"an OUTAGE is not a craft defect — it must not block the ship: {v7c['block']}"
            vision.compare_character = lambda png: {"consistent": True, "detail": ""}

            # REJECT — a finding the MACHINE can fix. The pairwise scan names the odd clip, and
            # the verdict carries the index + the reason so runner._regenerate can re-shoot it.
            # Clip numbering comes from the FILENAME, so a gap on disk cannot shift the blame.
            _scan({"flagged": [2], "whole_reel": False, "detail": "much more photoreal",
                   "people": {}, "person_detail": ""})
            v7d = run(d7, st)
            assert any("clip 2" in b and "different look" in b for b in v7d["block"]), \
                f"the odd clip must be NAMED in the block: {v7d['block']}"
            assert v7d["reject"] and v7d["reject"][0]["clips"] == [2], \
                f"the re-shoot must carry the clip index: {v7d.get('reject')}"
            assert "photoreal" in v7d["reject"][0]["reason"], \
                "the reason must travel into the re-shot clip's prompt"
            assert "reject" in v7d and "eye" in v7d, "both keys must exist on every verdict"
            # a minority can be re-shot; a MAJORITY differing means the anchor is the odd one out,
            # and re-shooting the majority is re-shooting the reel — that is the owner's call.
            _scan({"flagged": [1, 2], "whole_reel": True, "detail": "every scene differs",
                   "people": {}, "person_detail": ""})
            v7e = run(d7, st)
            assert not v7e["reject"], "a whole-reel difference must never auto-requeue"
            assert any("whole reel is one look" in g for g in v7e["gate"]), v7e["gate"]
            # and a down gateway is still an outage, never an auto-reject
            _scan(None)
            v7f = run(d7, st)
            assert not v7f["reject"] and any("clip-against-clip" in g for g in v7f["gate"]), v7f
            # the verdict names WHO judged it — a model's finding has to be attributable
            assert v7f.get("vision") == [], "no vision answer, no model claimed"
            _scan({"flagged": [], "whole_reel": False, "detail": "", "people": {},
                   "person_detail": ""}, model="a-named-model")
            v7g = run(d7, st)
            assert v7g["vision"] == ["a-named-model"], \
                f"the judging model must be recorded on the verdict: {v7g.get('vision')}"
            _scan({"flagged": [], "whole_reel": False, "detail": "", "people": {},
                   "person_detail": ""})

            # score-present: an EXISTING music_raw.mp3 is not evidence the bed is audible. Proven
            # against the real defect (darzi-ka-tukda, 2026-10-10): the bed was mixed, but the
            # track's 16.5s slow intro played through the hook, so the reel's first third was
            # silent while every offline check stayed green. Build one 12s reel with a loud bed
            # and one whose bed is a -70 dB whisper, and prove the floor tells them apart.
            for _name, _vol in (("bed_ok", 0.25), ("bed_gone", 0.0003)):
                _d = tempfile.mkdtemp()
                _cd = os.path.join(_CLIPS_ROOT, os.path.basename(_d))
                os.makedirs(_cd)
                for _i in range(3):
                    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d=4",
                                    "-f", "lavfi", "-i", "sine=frequency=440:d=4", "-pix_fmt",
                                    "yuv420p", "-shortest", os.path.join(_cd, f"clip_{_i:03d}.mp4")],
                                   check=True, capture_output=True)
                for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                    open(os.path.join(_d, _f), "wb").write(_PNG)
                open(os.path.join(_d, "music_raw.mp3"), "wb").write(b"x")
                json.dump({"opener": _name, "font": "Kalam", "quote": "Kabir",
                           "structure": f"arc {_name}"},
                          open(os.path.join(_d, "identity.json"), "w"))
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                                "-f", "lavfi",
                                "-i", f"sine=frequency=440:duration={_RL},volume={_vol}",
                                "-pix_fmt", "yuv420p", "-shortest",
                                os.path.join(_d, "final_veo.mp4")],
                               check=True, capture_output=True)
                _v = run(_d, st)
                _hit = [b for b in _v["block"] if "background score does not carry" in b]
                assert bool(_hit) == (_name == "bed_gone"), \
                    f"{_name} (bed at volume {_vol}): the score check must " \
                    f"{'flag' if _name == 'bed_gone' else 'pass'} it, blocks={_v['block']}"

            vision.compare_character = _orig_cmp

            # BY-EYE FALLBACK (WAVELENGTH §6, hard-never #8): when the ONLY thing wrong with a
            # reel is that a check couldn't run, the reel is shippable but NOT live until a human
            # has looked at THIS cut. d3 passes every auto check, so taking its eyes away (a down
            # vision gateway) produces exactly that case and nothing else.
            vision.review_frame = lambda png: None
            try:
                v8 = run(d3, st)
                assert v8["eye"] and sorted(v8["gate"]) == sorted(v8["eye"]), \
                    f"only the un-runnable checks may gate a by-eye reel: {v8['gate']}"
                assert not v8["ok"] and not v8["block"], v8
                assert not v8["live"], "an unsigned by-eye gap must not go live"
                assert not v8["eye_signed"], "nobody has looked yet"
                # the human's artifacts land in the project dir, next to the reel
                _j = json.load(open(os.path.join(d3, "eye_review.json"), encoding="utf-8"))
                assert _j["fp"] == Reel(d3).fp and _j["could_not_run"] == v8["eye"], _j
                assert os.path.exists(os.path.join(d3, _j["sheet"])), _j
                # a human looks and signs THIS cut -> live (still not `ok`: nothing was re-run)
                st.record_eye_review(Reel(d3).name, Reel(d3).fp, "owner", "looked at all panels")
                v8s = run(d3, st)
                assert v8s["eye_signed"] and v8s["live"] and not v8s["ok"], v8s
                # a signature is on a PICTURE, not on a project: rebuild the cut and it is void
                os.utime(Reel(d3).final, (1, 1))
                assert not run(d3, st)["live"], "a sign-off must not cover a different cut"
                # and a signature never clears a real defect, however fresh
                st.record_eye_review(Reel(d3).name, Reel(d3).fp, "owner", None)
                vision.review_frame = lambda png: [{"type": "extra-limb",
                                                    "detail": "a third arm"}]
                v8d = run(d3, st)
                assert v8d["eye_signed"] and v8d["gate"] and not v8d["eye"] and not v8d["live"], \
                    f"a signed reel with a REAL gated defect must still not go live: {v8d}"
                vision.review_frame = lambda png: []
                vision.medium_of = lambda png: "photoreal"     # the proven 3-D block path
                v8b = run(d3, st)
                assert v8b["eye_signed"] and v8b["block"] and not v8b["live"], v8b
                vision.medium_of = lambda png: "3d"
            finally:
                vision.review_frame = lambda png: []

            # caption WORDING — the layer that reads the words. Every other caption check is
            # geometry (safe area, on a face) or existence (craft layers), which is exactly why a
            # broken hook and a word spelled two ways reached the screen. Stub the judge so this
            # stays offline and prove all three outcomes: clean passes, judged-wrong gates, and a
            # down judge gates rather than passing or blocking.
            import agents
            _orig_judge = agents.judge_captions
            d9 = tempfile.mkdtemp()
            for _f in ("caption_hook.png", "caption_moral.png", "caption_cta.png"):
                open(os.path.join(d9, _f), "wb").write(_PNG)
            open(os.path.join(d9, "music_raw.mp3"), "wb").write(b"x")
            json.dump({"font": "rounded",
                       "hook": {"lines": ["उनकी मुस्कान बनें ख़ज़ाना"]},
                       "moral": {"lines": ["आपका एक मिनट उनका खज़ाना है"]},
                       "cta": {"lines": ["Standard Sunrise Foundation"]}},
                      open(os.path.join(d9, "captions.json"), "w"), ensure_ascii=False)
            open(os.path.join(d9, "script.md"), "w").write(
                "# t\n\nHOOK: Rich in life because… you stopped by.\n")
            json.dump({"opener": "a held breath", "font": "Kalam", "quote": "Bulleh Shah",
                       "structure": "the room empties"},
                      open(os.path.join(d9, "identity.json"), "w"))
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={_RL}",
                            "-f", "lavfi", "-i", f"sine=frequency=440:d={_RL}", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(d9, "final_veo.mp4")],
                           check=True, capture_output=True)
            try:
                agents.judge_captions = lambda s, c: {"problems": [], "captions": c}
                assert not any("caption wording" in g for g in run(d9, st)["gate"]), \
                    "a judge that finds nothing must not gate the reel"
                agents.judge_captions = lambda s, c: {"problems": ["बनें should be बने"],
                                                      "captions": c}
                assert any("caption wording" in g for g in run(d9, st)["gate"]), \
                    "a judged-wrong caption must gate go_live"
                # ...and it is the HUMAN's item, not a wall: a model's opinion of Hindi must not
                # stop a ship on its own, and re-rolling it until it agrees is not a way out.
                _v9 = run(d9, st)
                assert any("caption wording" in e for e in _v9["eye"]), \
                    f"a judge's objection must land in the human's list: {_v9['eye']}"
                assert not _v9["live"], "an unsigned objection must not go live"
                st.record_eye_review(Reel(d9).name, Reel(d9).fp, "owner", "read the cards")
                assert run(d9, st)["live"], \
                    "a human's sign-off must settle a judge's objection, not only an outage"

                def _down(s, c):
                    raise RuntimeError("all LLM gateways failed")

                agents.judge_captions = _down
                v9 = run(d9, st)
                assert any("could NOT be judged" in g for g in v9["gate"]), \
                    f"a down caption judge must gate, not fall silent: {v9['gate']}"
                assert not any("caption wording" in b for b in v9["block"]), \
                    f"an outage is not a caption defect — it must not block the ship: {v9['block']}"
            finally:
                agents.judge_captions = _orig_judge

            # the verdict is DURABLE state, keyed by reel, and a re-run overwrites it
            # (v6g, the GRANDFATHERED photoreal reel, is the passing one — v6, a NEW reel that
            #  declared photoreal, is now HELD; d3 is deliberately re-run as a defect just above,
            #  so its saved row is the HELD one, itself proof the upsert overwrites)
            assert st.qc_verdicts(v6g["reel"])[0]["ok"] == 1, "a passing verdict is persisted"
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
            vision.style_scan = _orig_scan
        st.close()
        print("OK: auto checks catch frozen/silent/off-safe-area + identity reuse; "
              "realism gates on a vision defect / reports a down gateway; clean reel passes")
        print("OK: verdict persisted to state.db; newest final*.mp4 is the one gated")
        print("OK: craft-layers holds a playable reel that ships no VO/moral/score")
        print("OK: pacing flags a stub reel and a 1s clip; character drift across scenes is HELD")
        print("OK: the caption judge gates a wrong Hindi card and reports a down gateway — "
              "never a silent pass, never a block; a human's sign-off settles the objection")
        print("OK: a check that could not run is shippable-not-live until a human signs THIS cut; "
              "a rebuild voids the signature and no signature clears a real defect")
    elif "--seed" in sys.argv:
        st = State(); seed(st)
        print(f"seeded {len(st.craft_lessons())} craft lessons into state.db"); st.close()
    elif "--signed-by" in sys.argv:
        # the human half of the by-eye fallback: record that a person looked at THIS cut, then
        # re-run the gate so the verdict (and `live`) reflects it.
        i = sys.argv.index("--signed-by")
        who = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        note = sys.argv[sys.argv.index("--note") + 1] if "--note" in sys.argv else None
        proj = next((a for a in sys.argv[1:] if not a.startswith("--")
                     and a not in (who, note)), None)
        if not proj or not who:
            raise SystemExit("usage: qc.py projects/<name> --signed-by '<name>' [--note '...']")
        st = State()
        r = Reel(proj)
        if not r.final:
            raise SystemExit(f"no final*.mp4 in {proj} — nothing to sign off")
        st.record_eye_review(r.name, r.fp, who, note)
        print(f"signed off '{r.name}' cut {r.fp} by {who}" + (f" — {note}" if note else ""))
        v = run(proj, st)
        _report(v)
        st.close()
        sys.exit(0 if v["live"] else 1)
    elif len(sys.argv) > 1:
        v = run(sys.argv[1])
        _report(v)
        sys.exit(0 if v["ok"] else 1)
    else:
        print(__doc__)
