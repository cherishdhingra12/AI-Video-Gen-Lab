"""assemble.py — the ONE assembler. clips -> a finished reel, with nothing hand-tuned per video.

Replaces the copy-per-video craft scripts (`projects/*/assemble_veo.py`). What it does:

  * joins the clips with cross-dissolves (a reel whose motif wants hard cuts sets XFADE=0)
  * finds the logo the site baked into the frame and inpaints it away, whatever site that was
  * adds the caption cards, each one PLACED by `caption.find_empty_band` over the frames it
    actually covers — the card goes where there is no face and no busy detail
  * lays an instrumental bed UNDER the clips' own audio
  * verifies the result actually plays, and heals the encode if it does not

It adds **no voiceover**: narration comes out of the generated clips (WAVELENGTH.md §3).

    python3 assemble.py projects/<name>
    XFADE=0 python3 assemble.py projects/<name>         # hard cuts

Cards come from an optional `captions.json` in the project:

    {"font": "rounded"|"serif",
     "hook":  {"lines": ["..."]},
     "moral": {"lines": ["..."]},
     "cta":   {"lines": ["..."]}}

Language follows WAVELENGTH.md: Devanagari or Roman Hinglish, whichever reads better.
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageFilter

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _DIR)

import caption                                                   # noqa: E402
import music as musiclib                                      # noqa: E402

W, H = 720, 1280                             # the 9:16 canvas every clip is fitted onto
FIT = f"scale={W}:{H}:force_original_aspect_ratio=decrease," \
      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"

# Every free site burns a small logo into the frame. It is the SAME mark in the SAME place for
# every clip of one reel (the fleet fails a whole reel over to the next site, never one clip) and
# the same overlay in every frame of it — which makes it FINDABLE instead of hard-coded: a static
# semi-transparent overlay compresses a pixel's temporal range by (1-alpha), so it sits in the
# range image as a dark blob of the mark's own shape. This replaces a box measured for one site
# only, which on another site smudged an innocent 72x74 patch while that site's own mark — a
# different place, a different size — survived untouched.
WM_ROI = (0.72, 0.78)      # x,y fraction: a baked logo lives in the bottom-right corner
WM_MIN_A = 0.12            # peak alpha below this = no mark, leave the clip alone
WM_MAX_PX = 4000           # a logo is small; a bigger "blob" is scene content, not an overlay
WM_FLAT = 0.45             # alpha inside the blob must be one flat plate, not an object's shading
WM_SHAPE = 0.5             # and it must be compact — a logo is a glyph, not a stripe

MUS_VOL = float(os.environ.get("MUS_VOL", "0.30"))
# Where a bed should sit IN THE MIX (dBFS), not a fixed multiplier: MUS_VOL=0.30 under the fleet's
# tracks (mastered near -21 dB) landed them near -31.5, so that is the target a bed is normalised
# to — a quietly-mastered track is lifted to it instead of landing doubly quiet. The gain is
# clamped, so an oddly-mastered track is nudged rather than blown up or buried.
BED_TARGET_DB = float(os.environ.get("BED_TARGET_DB", "-31.5"))
TAIL_FADE = float(os.environ.get("TAIL_FADE", "0.50"))
# Pace the whole reel. Generated motion often runs a touch slower than life, and the crafts used
# ~1.06x — but a pace change is a taste call on THAT reel, so this defaults to 1.0 (OFF) and is
# only applied when the reel asks for it (`speed` in identity.json, or SPEED=1.06 in the env).
# Video and audio are sped by the SAME factor, so lip-sync stays locked; atempo preserves pitch.
SPEED = float(os.environ.get("SPEED", "1.0"))
CARD_STYLE = dict(size=41, color="#FFF3D6", bg="rgba(18,16,14,0.72)")

# The edit is ONE span per clip, anchored on the voice (owner rule 2026-10-11: "start the video a
# little late ... a little bit before the vocals and a little bit after the vocals"). What this
# replaced returned the complement of every silence — 2-4 windows per clip — and spliced the
# picture at each seam; nine splices across five clips is what the owner saw as "very abrupt
# back-to-back cuts". Nothing inside the span is cut, however silent it measures: the picture
# decides that, not the sound, and measured on the dadi reel the "silent" hole in clip_000 is
# three seconds of her smiling straight at camera. TRIM=0 disables all of it.
TRIM = os.environ.get("TRIM", "1") != "0"
TRIM_DB = -35
PRE = float(os.environ.get("PRE", "0.50"))       # the beat kept before the first spoken line
POST = float(os.environ.get("POST", "0.60"))     # ... and after the last one
# 250-3500 Hz carries a human voice; a clip's own dynamic range in that band says whether it
# SPEAKS. The dadi reel measured 26.1-39.9 dB on its four spoken clips and 16.5 on the wordless
# sunrise sweep — and level alone cannot tell them apart, because the sweep's ambience bridges a
# -35dB silence gate and its loud fraction matches a spoken clip's.
VOICE_RANGE = float(os.environ.get("VOICE_RANGE", "20"))


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _silences(p):
    """The quiet windows of an audio file: [(start, end)] at TRIM_DB. The signal the whole
    trim/gap layer is built on — a silence is a fact about the sound, not an edit decision."""
    out = _run(["ffmpeg", "-hide_banner", "-i", p, "-af",
                f"silencedetect=noise={TRIM_DB}dB:d=0.30", "-f", "null", "-"]).stderr
    st = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    en = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    return list(zip(st, en))


def _merge(sil, bridge=0.50):
    """Silences with less than `bridge` of sound between them are ONE hole. Measured on clip_002,
    the sounds this bridges over are 19-29 dB below that clip's own line — room tone and creaks,
    not words. ponytail: a genuinely short (sub-0.5s) word spoken in a quiet passage would be
    bridged away with them; the fix is a level-aware bridge, and it needs a second clip that
    actually has one."""
    out = []
    for s, e in sil:
        if out and s - out[-1][1] <= bridge:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _has_voice(p):
    """Does this clip SPEAK? Answered by the clip's own dynamic range in the voice band, measured
    in 0.1s windows. False (or too short to measure) means no voice: the sound must not cut that
    clip at all — there the picture decides, and a human's eye settles it."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-af",
                          "highpass=f=250,lowpass=f=3500", "-f", "s16le", "-ac", "1", "-ar",
                          "16000", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    n, k = 1600, len(raw) // 2 // 1600                 # 0.1s windows at 16kHz
    if k < 5:
        return False
    db = 20 * np.log10(np.sqrt((a[:k * n].reshape(k, n) ** 2).mean(1)) + 1e-12)
    return float(np.percentile(db, 95) - np.percentile(db, 10)) >= VOICE_RANGE


def _keep_windows(p):
    """The ONE span of a clip to keep, in the clip's own time: from PRE before its first line to
    POST after its last, and nothing else. A single late start reads as one continuous shot; a cut
    inside the span reads as a jump, which is the jerk this replaced. Returns None when the clip
    has no voice (the picture decides there, not the sound) or when the span is the whole clip."""
    if not _has_voice(p):
        return None
    d = _dur(p)
    sil = _merge(_silences(p))
    if not sil:
        return None
    a = max(0.0, sil[0][1] - PRE) if sil[0][0] <= 0.15 else 0.0        # opens silent: start late
    b = min(d, sil[-1][0] + POST) if sil[-1][1] >= d - 0.15 else d     # closes silent: end early
    if b - a < 0.30 or (a <= 0.05 and b >= d - 0.05):
        return None
    return [(a, b)]


def compress_gaps(clips, keep_windows=None):
    """Rebuild each clip from its keep-window — the head, the tail and any long hole in ONE pass —
    and return (shortened clips, total cut seconds, [(head offset, new length)], wordless clips).

    Kept atoms are concatenated without time-stretching, so lip-sync cannot move and a line is
    never clipped mid-word. Doing it here is what lets the xfade chain and the card windows stay
    simple downstream — a clip is just a clip with a real length again, instead of a length plus a
    trim offset to track. A clip the sound may not cut comes back in `wordless` so the caller can
    say so out loud: "we could not decide this one, look at it"."""
    if not TRIM:
        return list(clips), 0.0, [(0.0, _dur(c)) for c in clips], []
    out, cut, info, wordless = [], 0.0, [], []
    for c in clips:
        wins = keep_windows(c) if keep_windows else _keep_windows(c)
        d = _dur(c)
        if not wins:
            if not _has_voice(c):
                wordless.append(c)
            out.append(c); info.append((0.0, d)); continue
        v = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in wins)
        a = v
        tmp = c + ".gap.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", c, "-filter_complex",
                            f"[0:v]select='{v}',setpts=N/FRAME_RATE/TB,fps=30[v];"
                            f"[0:a]aselect='{a}',asetpts=N/SR/TB[a]",
                            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-profile:v",
                            "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                            "-ar", "48000", "-ac", "2", "-movflags", "+faststart", tmp],
                           capture_output=True, text=True)
        nd = _dur(tmp) if r.returncode == 0 and os.path.exists(tmp) else d
        if nd >= d - 0.05:                     # nothing gained: keep the original, drop the copy
            if os.path.exists(tmp):
                os.remove(tmp)
            out.append(c); info.append((0.0, d)); continue
        cut += d - nd
        out.append(tmp)
        info.append((wins[0][0], nd))          # the head that was dropped, and the new length
    return out, cut, info, wordless


def _dur(p):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", p]).stdout
    return float(json.loads(out)["format"]["duration"])


def _has_audio(p):
    return "audio" in _run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", p]).stdout


def _mean_db(p, ss=None, dur=None):
    """Mean level in dBFS (ffmpeg volumedetect), whole file or one window. Recorded as a
    checkable artifact so QC can prove the score reached the mix, not just the disk."""
    cmd = ["ffmpeg", "-hide_banner"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    for ln in _run(cmd + ["-i", p, "-af", "volumedetect", "-f", "null", "-"]).stderr.splitlines():
        if "mean_volume:" in ln:
            try:
                return float(ln.split("mean_volume:")[1].split("dB")[0])
            except ValueError:
                return None
    return None


def _audio_floor(path, step=1.0):
    """The quietest 1s window's mean level — the reel's audio floor. A bed playing throughout
    keeps this up (a music bed sits ~-20..-30 dBFS, mixed ~-30..-40); a reel whose bed is absent
    or silent for a long stretch drops it far below the narration. Measured across the fleet
    2026-10-10: every healthy reel floors at -29..-41 dB, darzi (silent-intro bed) at -50.2, a
    no-bed build at -56.3. The gap is wide, so a floor threshold is a robust defect signal."""
    d = _dur(path)
    if not d:
        return None
    vals, t = [], 1.0
    while t < d - 1.2:
        lv = _mean_db(path, ss=t, dur=step)
        if lv is not None:
            vals.append(lv)
        t += step
    return round(min(vals), 2) if vals else None


def _frames_differ(p, d):
    import hashlib
    seen = set()
    for t in (d * 0.1, d * 0.35, d * 0.6, d * 0.85):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", p, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        seen.add(hashlib.md5(r.stdout).hexdigest())
    return len(seen) >= 2


def verify_playable(path, expect):
    probs = []
    d = _dur(path)
    if abs(d - expect) > 0.75:
        probs.append(f"duration {d:.2f}s != expected {expect:.2f}s")
    if not _frames_differ(path, d):
        probs.append("video is a frozen still (all sampled frames identical)")
    if not _has_audio(path):
        probs.append("no audio stream")
    elif _mean_db(path) < -60:
        probs.append("audio track is silent")
    return probs


def _sample(clip, fps=4.0):
    """A handful of frames from one clip, already FITTED to the reel canvas — the same FIT the
    filter graph applies, so a pixel here is the pixel `removelogo` will see."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", clip,
                        "-vf", f"{FIT},fps={fps}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                       capture_output=True)
    a = np.frombuffer(r.stdout, np.uint8)
    k = len(a) // (W * H * 3)
    return a[:k * W * H * 3].reshape(k, H, W, 3).astype(np.float32) if k else None


def _mark_mask(clips, work):
    """Find the logo the site baked into these clips. -> (pgm_path, info) or (None, None).

    Frames from EVERY clip are pooled into one temporal range image: the mark is static, so it is
    the same suppression in each of them, and pooling makes the range — and the alpha it implies —
    steadier than any single clip could.

    Removal is an INPAINT (`removelogo`), not an exact un-blend. An un-blend needs alpha and the
    mark's colour, and where the mark straddles a strong spatial edge — this reel's sparkle sits
    on the table leg — alpha, colour and the unknown background are not separable from one view:
    the least-squares fit of them was tried against the real clip and is ill-conditioned. The
    inpaint invents the ~800 px under the mark; a motion-compensated temporal fill would use real
    pixels instead and is the upgrade if this ever shows.
    """
    x0, y0 = int(W * WM_ROI[0]), int(H * WM_ROI[1])
    lo = hi = None
    for c in clips:
        f = _sample(c)
        if f is None or not len(f):
            continue
        lo = f.min(0) if lo is None else np.minimum(lo, f.min(0))
        hi = f.max(0) if hi is None else np.maximum(hi, f.max(0))
    if lo is None:
        return None, None
    rng = (hi - lo).mean(-1)
    # the background's OWN range, from a median far wider than the mark: read on a 4x downscale so
    # the ~124px box costs nothing and can never sit entirely inside a ~55px logo.
    ref = np.asarray(Image.fromarray(rng, "F").resize((W // 4, H // 4), Image.BOX)
                     .filter(ImageFilter.MedianFilter(31)).resize((W, H), Image.BILINEAR), np.float32)
    # ref < 3 is a background that does not move; a still background cannot tell us alpha, and
    # guessing one there would invent a mark in an empty corner.
    a = np.where(ref < 3.0, 0.0, np.clip(1.0 - rng / np.maximum(ref, 1e-3), 0.0, 0.95))
    roi = a[y0:, x0:]
    if roi.size == 0:
        return None, None
    py, px = np.unravel_index(np.argmax(roi), roi.shape)
    peak, cy, cx = float(roi[py, px]), y0 + int(py), x0 + int(px)
    if peak < WM_MIN_A:
        return None, None
    yy, xx = np.mgrid[0:H, 0:W]
    # the alpha contour at half its own peak IS the mark's silhouette; the disc drops a stray
    # speck of noise far from the peak. Dilated by 2px to take the anti-aliased rim with it — a
    # tight mask leaves the rim behind as a visible ghost outline of the star.
    m = (a > peak / 2) & ((xx - cx) ** 2 + (yy - cy) ** 2 <= 50 ** 2)
    m = np.asarray(Image.fromarray(m.astype(np.float32), "F")
                   .filter(ImageFilter.MaxFilter(5)), np.float32) > 0.5
    ys, xs = np.where(m)
    if not len(ys):
        return None, None
    # Guards against a bright STATIC thing in the corner being mistaken for an overlay: a logo is
    # small, compact, and its alpha is one flat plate — scene content is bigger, and either
    # sprawls into a stripe (testsrc's moving strip reads as alpha 0.95 down one band) or shades.
    w_, h_ = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    r = 0.5 * float(np.std(a[m])) / peak
    if len(ys) > WM_MAX_PX or r > WM_FLAT or not WM_SHAPE <= w_ / h_ <= 1 / WM_SHAPE:
        print(f"  bottom-right blob at ({cx},{cy}) is not an overlay "
              f"({w_}x{h_}px {len(ys)}px alpha {peak:.2f} spread {r:.2f}) — left alone")
        return None, None
    # the argmax lands on one noisy pixel of the blob; the mask's own box is what the mark's
    # position actually is, and it is that box the report and the self-check should name.
    cx, cy = int(xs.min() + w_ // 2), int(ys.min() + h_ // 2)
    pgm = os.path.join(work, "wm_mask.pgm")
    with open(pgm, "wb") as fh:
        fh.write(b"P5\n%d %d\n255\n" % (W, H))
        fh.write((m * 255).astype(np.uint8).tobytes())
    return pgm, {"cx": cx, "cy": cy, "alpha": round(peak, 3), "px": int(len(ys)),
                 "box": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]}


def _wm_filter(pgm):
    # a filter argument list is ':'-separated, so a colon in the path has to be escaped
    return "removelogo=filename=" + pgm.replace("\\", "\\\\").replace(":", "\\:")


def _heal(path):
    tmp = path + ".heal.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-profile:v", "high",
                    "-level", "4.0", "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", "30",
                    "-video_track_timescale", "90000", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2", "-movflags", "+faststart", tmp],
                   check=True, capture_output=True)
    os.replace(tmp, path)


def _vision():
    import vision
    return vision


class StyleDrift(Exception):
    """A build STOPPED because named clips do not match the reel's look.

    It carries the clip paths and the reason because the caller's job is NOT to try the same build
    again — that was the old behaviour and it livelocked: assemble raised, the stitch loop deleted
    the partial cut and rebuilt the identical cut from the identical clips, forever. The caller
    re-shoots THESE clips, so the finding has to leave the build with the names on it."""

    def __init__(self, bad):
        self.bad = list(bad)                       # [(clip_path, reason)]
        self.reason = "; ".join(f"{os.path.basename(p)} {r}" for p, r in self.bad)
        super().__init__(f"BUILD REJECTED — style drift; requeue {len(self.bad)} clip(s): "
                         f"{self.reason}")


def style_preflight(clips):
    """The prompt saying '3-D' is not evidence — the RENDERED frame is. One ambiguous frame (an
    AR filter over a face, a phone-screen inset) must not burn a re-render, while a real
    photoreal clip is always caught.

    Returns [] when the look holds, else [(clip_path, reason)] naming the clips to re-shoot
    (raise StyleDrift on it, don't guess which clip the message meant).

    TWO questions, because they fail separately: (1) is each clip the house medium, classified
    per clip; (2) do the clips match EACH OTHER, asked pairwise against the first clip. (2) is
    not redundant — `medium_of` buckets a frame four ways and read a whole photoreal clip as '3d'
    next to 3-D neighbours, which is how a reel arrived at the gate carrying three different
    renders (dadi-ki-dhansuchi, 2026-10-12). The first frame of each clip is asked both questions,
    so the second one costs no extra ffmpeg call.
    """
    if os.environ.get("STYLE_CHECK") == "0":
        print("style pre-flight skipped (STYLE_CHECK=0)")
        return []
    vis = _vision()
    bad, frames = [], []
    for p in clips:
        samples, first = [], None
        for frac in (0.35, 0.7):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{_dur(p) * frac:.2f}", "-i", p,
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True)
            if r.stdout:
                samples.append(vis.medium_of(r.stdout))
                first = first or r.stdout
        verdict, ev = vis.medium_verdict(samples, expected="3d")
        print(f"  {os.path.basename(p)} -> {verdict} ({samples})")
        if verdict in ("drift", "wrong-medium"):
            bad.append((p, ev))
        elif verdict == "unknown":
            print(f"  WARNING: {os.path.basename(p)} could not be classified — check it by eye")
        if first:
            frames.append((p, first))
    if len(frames) >= 2:
        scan = vis.style_scan([f for _, f in frames])
        if scan is None:
            print("  WARNING: clip-against-clip look could not be reviewed — check it by eye")
        elif scan["whole_reel"]:
            # MORE than half differ from clip 0, so clip 0 is the odd one out — re-shooting the
            # rest would be re-shooting the reel. Never auto-rejected: that is the owner's call.
            print(f"  NOTE: every clip differs from the first ({scan['detail']}) — that is the "
                  "whole reel's look, not one bad clip; building, the owner settles this")
        else:
            for i in scan.get("unreviewed") or []:
                # a clip whose sampled frame holds no person: the look check had nothing to
                # compare, which is not a pass — say so, and let the eye be the backstop.
                print(f"  WARNING: clip {i} could not be compared to clip 0 (no person in its "
                      f"frame) — check it by eye")
            for i in scan["flagged"]:
                bad.append((frames[i][0], f"does not match the reel's look ({scan['detail']})"))
    if bad:
        print("STYLE DRIFT — refusing to build: "
              + "; ".join(f"{os.path.basename(p)}: {e}" for p, e in bad))
    else:
        print("style pre-flight OK — one look across every clip")
    return bad


def _absorb(project_dir):
    """Re-audit what actually shipped and file a candidate for any layer that is missing.
    Fail-open: bookkeeping must never turn a finished build into a failed one."""
    try:
        sys.path.insert(0, ROOT)          # brain.py lives at the project root, not here — the
        import brain                      # import has to come AFTER the path is set, or a build
        r = brain.LocalBrain().absorb(project_dir)   # run from automation/ silently never absorbs
        if r.get("ok"):
            gap = ("filed " + ", ".join(r["learned"])) if r.get("learned") else "all layers present"
            print(f"  brain absorbed: {gap}")
    except Exception as e:  # noqa: BLE001
        print(f"  brain absorb skipped ({str(e)[:110]})")


def _card_height(lines, font, width):
    """The card's real ink height — shared with publishing.cover, which places a card over a
    single frame and must ask for the band with the same number the reel did."""
    return caption.card_height(lines, font, width, **CARD_STYLE)


def _place(lines, font, width, src_clip, t0, t1):
    """Write the card into an empty spot over `src_clip` between t0 and t1, and return the PNG.
    Falls back to the top strip when the frames cannot be read at all."""
    h = _card_height(lines, font, width)
    y = caption.find_empty_band(src_clip, t0, t1, h) if os.path.exists(src_clip) else None
    if y is None:
        print("    placement fell back to the top strip (frames unreadable)")
        y = 96
    print(f"    placed at y={y} (card {h}px tall)")
    return y, h


def _spec(project_dir):
    p = os.path.join(project_dir, "captions.json")
    if not os.path.exists(p):
        return {}, "rounded"
    d = json.load(open(p, encoding="utf-8"))
    return d, d.get("font", "rounded")


def _speed(project_dir):
    """The reel's own pace, if it declared one. An env override wins (hand-tuning a single
    reel); otherwise `speed` from identity.json; otherwise 1.0, i.e. leave the pace alone."""
    if os.environ.get("SPEED"):
        return SPEED
    try:
        s = float(json.load(open(os.path.join(project_dir, "identity.json"),
                                 encoding="utf-8")).get("speed") or 1.0)
    except Exception:  # noqa: BLE001 — no identity, or not a number: keep the native pace
        return 1.0
    return s if 0.5 <= s <= 2.0 else 1.0      # outside this, atempo would need chaining


def build(project_dir, clips_dir=None, absorb=True):
    project_dir = os.path.abspath(project_dir)
    name = os.path.basename(project_dir)
    clips_dir = clips_dir or os.path.join(ROOT, "clips", name)
    clips = sorted(glob.glob(os.path.join(clips_dir, "clip_*.mp4")))
    if not clips:
        raise RuntimeError(f"no clips in {clips_dir}")
    out = os.path.join(project_dir, "final_veo.mp4")
    n = len(clips)
    durs = [_dur(c) for c in clips]
    xf = float(os.environ.get("XFADE", "0.40"))

    print("style pre-flight…")
    drift = style_preflight(clips)
    if drift:
        raise StyleDrift(drift)

    # ---- one pass turns each generated clip into what it should have been: the ends trimmed and
    #      the dead air between spoken lines compressed. `clips` becomes the shortened files and
    #      every downstream window is in this new timeline, so nothing has to carry a trim offset.
    clips, cut, winfo, wordless = compress_gaps(clips)
    durs = [_dur(c) for c in clips]
    eff = list(durs)
    if cut:
        print(f"  dead air: cut {cut:.2f}s (started late, ended early — no splice inside a clip)")
    if wordless:
        print(f"  no vocals in {len(wordless)} clip(s) — left whole, because there the picture "
              f"decides and not the sound: {', '.join(os.path.basename(w) for w in wordless)}")

    work = tempfile.mkdtemp(prefix="reelwm-")
    wm_pgm, wm = _mark_mask(clips, work)
    if wm:
        print(f"  baked logo at ({wm['cx']},{wm['cy']}) alpha {wm['alpha']}, {wm['px']}px, "
              f"box {wm['box']} — inpainted away")
    else:
        print("  no baked logo found — clips left untouched")

    fc, inputs = [], []
    for c in clips:
        inputs += ["-i", c]
    # `offset` is where the dissolve STARTS in the accumulated stream, so the FIRST clip
    # contributes its full length — only the joins shorten the reel. Subtracting xf from the
    # first segment too (as this did) started every dissolve xf too early: the xfade video chain
    # then ran xf short PER JOIN while the acrossfade audio chain did not, so a 6-clip reel ended
    # ~2s of picture before its sound, and every card window was timed against a reel that did
    # not exist. Verified against ffprobe: 3 clips of 5s, xf 0.4 -> 14.2s on both streams.
    seg = [eff[0]] + [eff[i] - xf for i in range(1, n)]
    for i in range(n):
        # FIT is an identity for a clip that is already 9:16, and saves the reel when a site
        # returns a square or 16:9 generation — a mixed-size join otherwise mis-frames silently.
        vf = f"{FIT},format=yuv420p,fps=30,setsar=1"
        if wm_pgm:
            vf += "," + _wm_filter(wm_pgm)
        fc.append(f"[{i}:v]{vf}[v{i}]")
        af = "aformat=sample_rates=48000:channel_layouts=stereo"
        if _has_audio(clips[i]):
            af += ",asetpts=PTS-STARTPTS"
            fc.append(f"[{i}:a]{af}[a{i}]")
        else:
            fc.append("anullsrc=channel_layout=stereo:sample_rate=48000,"
                      f"atrim=duration={eff[i]:.3f},asetpts=PTS-STARTPTS[a{i}]")
    vprev, aprev, L = "v0", "a0", seg[0]
    starts = [0.0]
    for i in range(1, n):
        off = L - xf
        fc.append(f"[{vprev}][v{i}]xfade=transition=fade:duration={xf}:offset={off:.3f}[vx{i}]")
        fc.append(f"[{aprev}][a{i}]acrossfade=d={xf}:c1=tri:c2=tri[ax{i}]")
        vprev, aprev = f"vx{i}", f"ax{i}"
        starts.append(off)
        L += seg[i]
    total = L

    # ---- cards: hook over the opener, a moral, and the CTA last ----
    # Each is PLACED against the source clip it sits over, in that clip's own local time.
    # Fixed offsets off the end of the reel collapse on a short closer (a 5s last clip left the
    # moral 0.25s, which is worse than no card), so the moral falls back to the clip before the
    # closer when there is not room for both it and the CTA.
    spec, font = _spec(project_dir)
    windows = []
    if n >= 2:
        windows.append(("hook", 0, 0.45, min(starts[1] - 0.20, 8.0)))
        cta_len = min(3.4, max(1.6, total * 0.22))
        c0, c1 = total - 0.35 - cta_len, total - 0.35
        mi, m0 = n - 1, max(starts[-1] + 0.55, 0.5)
        m1 = min(m0 + 6.4, c0 - 0.6)
        if m1 - m0 < 1.8 and n >= 3:
            mi, m0 = n - 2, starts[-2] + 0.55
            m1 = min(m0 + 6.4, starts[-1] + 0.4)
        windows.append(("moral", mi, m0, m1))
        windows.append(("cta", n - 1, c0, c1))

    vmap, ninp, cards = vprev, n, []
    placed = {}
    for kind, ci, cs, ce in windows:
        lines = (spec.get(kind) or {}).get("lines") or []
        if ce <= cs or not lines:
            continue
        print(f"  card {kind}: {cs:.2f}-{ce:.2f}s (over clip {ci})")
        # the window is in REEL time; the clip it sits over has already been trimmed in this
        # timeline, so the sample window is just the offset back into that clip
        y, h = _place(lines, font, 560, clips[ci], max(0.0, cs - starts[ci]),
                      max(0.1, ce - starts[ci]))
        png = os.path.join(project_dir, f"caption_{kind}.png")
        caption.render(lines, png, y=y, font=font, **CARD_STYLE)
        # Wik the card's real ink box and the window it occupies so QC can re-ask "is there a face
        # under this?" against the shipped cut, without guessing where the card is.
        _ink = np.asarray(Image.open(png).convert("RGBA").getchannel("A"))
        _ys, _xs = np.where(_ink > 150)
        if len(_ys):
            placed[kind] = {"start": round(cs, 3), "end": round(ce, 3),
                            "y0": int(_ys.min()), "y1": int(_ys.max()),
                            "x0": int(_xs.min()), "x1": int(_xs.max())}
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
        oi = ninp
        ninp += 1
        fc.append(f"[{oi}:v]format=rgba,fade=t=in:st={cs:.3f}:d=0.4:alpha=1,"
                  f"fade=t=out:st={ce - 0.4:.3f}:d=0.4:alpha=1[capv{oi}]")
        fc.append(f"[{vmap}][capv{oi}]overlay=0:0:enable='between(t,{cs:.3f},{ce:.3f})'[vo{oi}]")
        vmap = f"vo{oi}"
        cards.append(kind)

    if placed:                       # where each card landed, for QC to re-check by face
        # The window is in PRE-speed reel time; QC samples these times against the SHIPPED cut,
        # so divide by the pace (below) before writing or the check reads the wrong frames.
        try:
            _sp = _speed(project_dir)
            conv = {k: {**w, "start": round(w["start"] / _sp, 3), "end": round(w["end"] / _sp, 3)}
                    for k, w in placed.items()}
            with open(os.path.join(project_dir, "caption_place.json"), "w") as f:
                json.dump(conv, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ---- gentle fade-out so the closing shot never just stops ----
    if TAIL_FADE > 0:
        st = total - TAIL_FADE
        fc.append(f"[{vmap}]fade=t=out:st={st:.3f}:d={TAIL_FADE:g}[vout]")
        fc.append(f"[{aprev}]afade=t=out:st={st:.3f}:d={TAIL_FADE:g}[atail]")
        vmap, aprev = "vout", "atail"

    # ---- the bed sits UNDER the clips' own audio (which carries the narration), never
    #      instead of it ----
    amap, scored = aprev, False
    music = os.path.join(project_dir, "music_raw.mp3")
    if os.path.exists(music):
        # Placement is MEASURED, not assumed. A slow intro (a raga's alap) is silence for many
        # seconds — starting there plays the reel's hook with no music (the darzi-ka-tukda
        # defect: ~14s inaudible). music.analyse measures where the track reaches its body and
        # music_meta.json carries it; old projects without the sidecar fall back to t=0.
        skip = 0.0
        bed_mean = None
        try:
            with open(os.path.join(project_dir, "music_meta.json")) as f:
                meta = json.load(f)
            skip = max(0.0, float(meta.get("level_start") or 0.0))
            bed_mean = meta.get("mean_db")
        except (OSError, ValueError, TypeError):
            # No sidecar (a project from before it existed, or a hand-installed bed): MEASURE the
            # track rather than falling back to t=0. Falling back is what shipped darzi silent —
            # the sidecar was absent, so nothing knew the track opens on a 16.5s alap.
            try:
                m = musiclib.analyse(music)
                skip = max(0.0, float(m.get("level_start") or 0.0))
                bed_mean = m.get("mean_db")
            except Exception:  # noqa: BLE001 — an unreadable bed still mixes, from its head
                pass
        # Feed forward from the skip point; if the skip point alone cannot fill the reel (the
        # body is shorter than the reel), start at 0 instead of running the tail dry.
        mi = ninp
        inputs += ["-i", music]
        ninp += 1
        if skip >= 0.2 and (_dur(music) - skip) < total:
            skip = 0.0
        # Normalise the bed to a TARGET level in the mix rather than a fixed multiplier: a
        # quietly-mastered track must not land doubly quiet under a fixed 0.30. Clamped, because
        # a measured -40 dB bed would otherwise be pushed to a level that swamps the narration.
        vol = MUS_VOL
        if isinstance(bed_mean, (int, float)):
            vol = min(0.60, max(0.06, 10 ** ((BED_TARGET_DB - float(bed_mean)) / 20)))
        if skip >= 0.2:
            print(f"  bed: skipping the track's {skip:.1f}s quiet intro")
        if abs(vol - MUS_VOL) > 1e-3:
            print(f"  bed: level {vol:.2f} (track measures {bed_mean:+.1f} dB, "
                  f"target {BED_TARGET_DB:+.0f})")
        fc.append(f"[{mi}:a]atrim={skip:.3f}:{skip + total:.3f},asetpts=PTS-STARTPTS,"
                  "aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"volume={vol:g},afade=t=in:st=0:d=1.4,"
                  f"afade=t=out:st={max(0.0, total - 1.6):.3f}:d=1.6[bed]")
        fc.append(f"[{aprev}][bed]amix=inputs=2:normalize=0:dropout_transition=0[scored]")
        amap, scored = "scored", True

    # ---- pace, last: every window above is in REEL time, and speeding the finished composite
    #      keeps them all valid (a card simply stays up for (ce-cs)/speed seconds). Video and
    #      audio move together, so lip-sync is untouched; atempo holds the pitch. ----
    speed = _speed(project_dir)
    out_len = total
    if abs(speed - 1.0) > 1e-6:
        fc.append(f"[{vmap}]setpts=(PTS-STARTPTS)/{speed:g}[vsp]")
        # `amap` (post-mix), not `aprev`: the bed is already folded into the mix by this point,
        # so speeding the mix keeps speech and score locked together instead of desyncing them
        fc.append(f"[{amap}]atempo={speed:g}[asp]")
        vmap, amap = "vsp", "asp"
        out_len = total / speed
        print(f"  pace: {speed:g}x -> {out_len:.2f}s")

    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", f"[{vmap}]", "-map", f"[{amap}]",
                    "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
                    "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", "30",
                    "-video_track_timescale", "90000", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out],
                   check=True, capture_output=True)
    for c in clips:                       # the compressed temp copies are scratch, always
        if c.endswith(".gap.mp4") and os.path.exists(c):
            os.remove(c)

    probs = verify_playable(out, out_len)
    if probs:
        print("verify FAILED:", "; ".join(probs), "\n  -> healing (QuickTime-safe re-mux)…")
        _heal(out)
        probs = verify_playable(out, out_len)
        if probs:
            raise RuntimeError("BUILD REJECTED — still broken after heal: " + "; ".join(probs))
        print("  healed OK.")
    layers = [f"captions x{len(cards)}", "score" if scored else "NO SCORE",
              "no VO (narration is in the clips)"]
    if cut:
        layers.append(f"dead-air trimmed {cut:.2f}s")
    if abs(speed - 1.0) > 1e-6:
        layers.append(f"{speed:g}x pace")
    if scored:
        # Record the audio's floor in the FINAL as a checkable artifact: the existence of
        # music_raw.mp3 says nothing about whether the score is audible in the reel (the hole
        # that let darzi's silent-intro bed ship). QC reads this instead of guessing.
        try:
            with open(os.path.join(project_dir, "music_bed.json"), "w") as f:
                json.dump({"mean_db": _mean_db(out), "floor": _audio_floor(out)}, f)
        except OSError:
            pass
    print(f"OK -> {out}  ({_dur(out):.2f}s, {n} clips, {', '.join(layers)})  VERIFIED")
    # ---- the folder is the deliverable: the cover and the publishing sheet are part of the
    #      build, so the next reel has them because the pipeline wrote them (see publishing.py).
    #      Never fatal and never silent: the cut, the gate and the sign-off are unaffected.
    try:
        import publishing
        publishing.pack(project_dir)
    except Exception as e:  # noqa: BLE001 — a missing pack must not cost a finished cut
        print(f"  publishing pack skipped ({str(e)[:90]}) — write it later with "
              f"python3 automation/publishing.py {project_dir}")
    if absorb:
        _absorb(project_dir)
    return out


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        os.environ["STYLE_CHECK"] = "0"                 # offline: no vision gateway
        os.environ["PUBLISH_LLM"] = "0"                 # offline: no LLM for the publish sheet
        _absorb = lambda _d: None                       # never touch the real brain from a test
        d = tempfile.mkdtemp()
        pdir, cdir = os.path.join(d, "demo"), os.path.join(d, "clips", "demo")
        xdir = os.path.join(d, "trim")                  # trim fixtures: NOT clips of the reel
        os.makedirs(pdir)
        os.makedirs(cdir)
        os.makedirs(xdir)
        for i in range(3):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=5",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=5",
                            "-pix_fmt", "yuv420p", "-shortest",
                            os.path.join(cdir, f"clip_{i:03d}.mp4")],
                           check=True, capture_output=True)

        def _clip(name, secs, af):
            p = os.path.join(xdir, name)
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=720x1280:d={secs}",
                            "-f", "lavfi", "-i", af, "-pix_fmt", "yuv420p", "-shortest",
                            "-c:a", "aac", p], check=True, capture_output=True)
            return p

        # clip 0 of the reel is shaped like a generated clip really ships: 1.2s of nothing at the
        # head, a line, a 0.95s pause, a second line, 3.4s of nothing at the tail. The head and
        # the tail go; the pause between the two lines is a breath and MUST survive — the rule
        # this replaced spliced it, 2-4 times per clip, and the owner saw "abrupt back-to-back
        # cuts" (nine of them across one reel).
        say = ("0.5*sin(2*PI*300*t)*(between(mod(t\\,9)\\,1.2\\,2.6)"
               "+between(mod(t\\,9)\\,3.55\\,5.0))")
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=9",
                        "-f", "lavfi", "-i", f"aevalsrc={say}:d=9",
                        "-pix_fmt", "yuv420p", "-shortest",
                        os.path.join(cdir, "clip_000.mp4")],
                       check=True, capture_output=True)
        kw = _keep_windows(os.path.join(cdir, "clip_000.mp4"))
        assert kw and len(kw) == 1, f"ONE span per clip, never a splice inside it ({kw})"
        assert 0.4 <= kw[0][0] <= 0.8, f"must start ~PRE before the line, got {kw[0][0]:.2f}"
        assert 5.3 <= kw[0][1] <= 5.9, f"must end ~POST after the line, got {kw[0][1]:.2f}"
        assert kw[0][0] < 1.2 and kw[0][1] > 5.0, \
            f"the span must RUN THROUGH both lines and the pause between them, got {kw[0]}"
        # A clip that speaks and has no dead air at all is left whole too (dadi clip_001): the
        # gate opens and shuts faster than silencedetect's 0.30s, so there is nothing to find.
        solid = _clip("solid.mp4", 5, "aevalsrc=0.5*sin(2*PI*300*t)*between(mod(t\\,0.4)\\,0.0\\,0.2):d=5")
        assert _has_voice(solid), "a gated line must still read as a voice"
        assert _keep_windows(solid) is None, \
            "a clip that speaks with no dead air must be left completely alone"
        # No voice at all: steady ambience, no line. The sound must not cut it — there the picture
        # decides and a human's eye settles it (owner: "taken care of by seeing the video"). Level
        # alone cannot tell these apart, so the dynamic range of the voice band must.
        wordless = _clip("wordless.mp4", 5, "anoisesrc=c=pink:a=0.02:d=5")
        assert not _has_voice(wordless), "steady ambience is not a voice, however loud it is"
        assert _has_voice(os.path.join(cdir, "clip_000.mp4")), "a spoken clip must read as spoken"
        assert _keep_windows(wordless) is None, "a clip with no voice must never be cut by the sound"
        assert len(compress_gaps([wordless])[3]) == 1, \
            "a wordless clip must be reported, not silently left whole"
        print("OK: one span per clip — start late, end early, the pause inside left alone, and a "
              "wordless clip untouched")
        # ---- the baked site logo is FOUND, never hard-coded (and a clean corner is left alone).
        # A scrolling ramp gives every pixel a wide, uniform temporal range; a translucent star is
        # drawn over the corner a site burns its mark into, then the detector must place it there.
        yy, xx = np.mgrid[0:H, 0:W]
        star = ((np.abs(xx - 600) / 26.0) ** (2 / 3)
                + (np.abs(yy - 1150) / 26.0) ** (2 / 3)) <= 1.0

        def _ramp(name, alpha):
            p = os.path.join(d, name)
            enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo",
                                    "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", "25",
                                    "-i", "-", "-crf", "10", "-pix_fmt", "yuv420p", p],
                                   stdin=subprocess.PIPE)
            for t in range(40):
                base = (255.0 * ((np.arange(W) + t * 18) % W) / W)[None, :, None]
                fr = base * np.ones(3)[None, None, :] * (1 - alpha * star[..., None])
                enc.stdin.write(np.clip(fr, 0, 255).astype(np.uint8).tobytes())
            enc.stdin.close()
            enc.wait()
            return p

        _, wm = _mark_mask([_ramp("wm.mp4", 0.40)], d)
        assert wm and abs(wm["cx"] - 600) <= 6 and abs(wm["cy"] - 1150) <= 6, \
            f"the baked logo was not found where it was drawn: {wm}"
        assert abs(wm["alpha"] - 0.40) < 0.10, f"the logo's alpha was misread: {wm['alpha']}"
        assert 500 < wm["px"] < 1600, f"the logo's silhouette is the wrong size: {wm['px']}px"
        print(f"OK: the baked logo is found from the clips alone — ({wm['cx']},{wm['cy']}) "
              f"alpha {wm['alpha']}, {wm['px']}px, box {wm['box']} — not a hard-coded box")
        assert _mark_mask([_ramp("plain.mp4", 0.0)], d) == (None, None), \
            "a corner with nothing static in it must NOT be inpainted"
        print("OK: a clean corner yields no mask, so the clip is passed through untouched")
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:d=20",
                        "-c:a", "libmp3lame", os.path.join(pdir, "music_raw.mp3")],
                       check=True, capture_output=True)
        with open(os.path.join(pdir, "captions.json"), "w", encoding="utf-8") as f:
            json.dump({"font": "rounded",
                       "hook": {"lines": ["हर हफ़्ते की लिस्ट पर", "एक ही चीज़…"]},
                       "moral": {"lines": ["वो तोहफ़े नहीं,", "आपका वक़्त"]},
                       "cta": {"lines": ["Standard Sunrise Foundation"]}}, f)

        out = build(pdir, clips_dir=cdir)
        assert os.path.exists(out), "no reel built"
        assert not verify_playable(out, _dur(out)), "the built reel must actually play"
        # the trimmed reel must be shorter than the naive join of the untrimmed clips
        naive = sum(_dur(c) for c in glob.glob(os.path.join(cdir, "clip_*.mp4"))) - 2 * 0.40
        assert _dur(out) < naive - 1.2, \
            f"dead air was not cut: reel {_dur(out):.2f}s vs untrimmed {naive:.2f}s"
        got = sorted(f for f in os.listdir(pdir)
                   if f.startswith("caption_") and f.endswith(".png"))
        assert got == ["caption_cta.png", "caption_hook.png", "caption_moral.png"], got
        for f in got:
            a = np.asarray(Image.open(os.path.join(pdir, f)).convert("RGBA"))[..., 3]
            assert a.max() == 255, f"{f}: the card did not render"
            assert a[int(0.85 * 1280):].max() == 0, f"{f} leaks into the bottom 15%"
        print(f"OK: 3 clips -> a reel that plays ({_dur(out):.2f}s, dead air cut), "
              "three auto-placed cards, none in the bottom 15%")

        # ---- the bed: offline QC used to see only that music_raw.mp3 exists, which passed a
        # reel whose bed was silent through its hook (darzi-ka-tukda). Two things must hold, and
        # only a real build proves them: a bed placed with NO sidecar is MEASURED (not started
        # blindly at t=0), and the finished reel carries its own audibility proof — a floor.
        def _bedfloor(path_):
            import qc
            return qc._audio_floor(path_)

        plain_floor = _bedfloor(out)
        assert plain_floor is not None and plain_floor > -46, \
            f"a reel with a real bed must floor above the defect threshold: {plain_floor}"
        assert os.path.exists(os.path.join(pdir, "music_bed.json")), \
            "the build must record the bed's floor for QC to read"

        # now the defect itself: a bed that opens on a long quiet intro (a raga's alap). With no
        # music_meta.json the assembler must MEASURE where the track reaches its body and skip the
        # intro — the old code started at t=0 and played the reel's whole hook in silence.
        #
        # The reel's own narration has to have GAPS for this to be provable. The source clips above
        # carry a continuous tone, so a bed covering the first three seconds is indistinguishable
        # from one covering the hook. Real narration is spoken lines with silence between them, and
        # the silence must sit in the MIDDLE of a clip — trailing silence is dead air and gets cut.
        # This narration speaks 0-1.25s and 3.75-5s, leaving a 2.5s hole the bed has to fill.
        _gate = "0.5*sin(2*PI*440*t)*(lt(mod(t\\,5)\\,1.25)+gt(mod(t\\,5)\\,3.75))"
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:d=3",
                        "-f", "lavfi", "-i", "sine=frequency=330:d=25", "-filter_complex",
                        "[0:a]volume=0.004[quiet];[quiet][1:a]concat=n=2:v=0:a=1[out]",
                        "-map", "[out]", "-c:a", "libmp3lame",
                        os.path.join(pdir, "music_raw.mp3")], check=True, capture_output=True)
        os.remove(os.path.join(pdir, "music_meta.json")) \
            if os.path.exists(os.path.join(pdir, "music_meta.json")) else None
        for _i in range(3):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=5",
                            "-f", "lavfi", "-i", f"aevalsrc={_gate}:d=5", "-pix_fmt", "yuv420p",
                            "-shortest", os.path.join(cdir, f"clip_{_i:03d}.mp4")],
                           check=True, capture_output=True)
        alap = build(pdir, clips_dir=cdir)
        m = musiclib.analyse(os.path.join(pdir, "music_raw.mp3"))
        assert m["level_start"] >= 2.0, \
            f"the fixture's quiet intro must be measured (got {m['level_start']}s)"
        assert _bedfloor(alap) > -46, \
            f"a reel whose bed has a quiet intro must still floor above the threshold — the " \
            f"intro should have been skipped, not played: {_bedfloor(alap)}"
        print(f"OK: the bed's audibility is measured from the FINAL (floor {plain_floor:.1f} dB); "
              f"a {m['level_start']:.1f}s quiet intro is skipped, not played through the hook")

        # ---- pace: the reel's own declared speed shortens the reel and keeps every card ------
        json.dump({"opener": "a held breath", "font": "rounded", "quote": "Kabir",
                   "structure": "bookend", "speed": 1.25},
                  open(os.path.join(pdir, "identity.json"), "w", encoding="utf-8"))
        plain = _dur(out)
        fast = build(pdir, clips_dir=cdir)
        assert abs(_dur(fast) - plain / 1.25) < 1.2, \
            f"1.25x pace did not shorten the reel: {_dur(fast):.2f}s vs {plain:.2f}s"
        assert not verify_playable(fast, _dur(fast)), "a paced reel must still play"
        got = sorted(f for f in os.listdir(pdir)
                   if f.startswith("caption_") and f.endswith(".png"))
        assert got == ["caption_cta.png", "caption_hook.png", "caption_moral.png"], got
        # ...and a speed outside what atempo can do in one stage is ignored, not obeyed blindly
        assert _speed(pdir) == 1.25, "the reel's declared speed must be read"
        # the recorded card windows are what QC samples against the SHIPPED cut, so a paced reel
        # must record POST-speed times — a pre-speed window can name a moment past the reel's end
        place = json.load(open(os.path.join(pdir, "caption_place.json"), encoding="utf-8"))
        for k, w in place.items():
            assert w["end"] <= _dur(fast) + 0.2, \
                f"caption_place {k} ends at {w['end']}s, past the paced reel ({_dur(fast):.2f}s)"
        json.dump({"speed": 9.0}, open(os.path.join(pdir, "identity.json"), "w"))
        assert _speed(pdir) == 1.0, "an unusable speed must fall back to the native pace"
        print(f"OK: declared pace applied ({plain:.2f}s -> {_dur(fast):.2f}s), "
              "cards intact, an out-of-range speed ignored")
        raise SystemExit(0)

    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 assemble.py projects/<name>")
    build(sys.argv[1])
