"""assemble.py — the ONE assembler. clips -> a finished reel, with nothing hand-tuned per video.

Replaces the copy-per-video craft scripts (`projects/*/assemble_veo.py`). What it does:

  * joins the clips with cross-dissolves (a reel whose motif wants hard cuts sets XFADE=0)
  * strips the baked Vids sparkle
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
from PIL import Image

_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_DIR)
sys.path.insert(0, _DIR)

import caption                                                   # noqa: E402

DELOGO = "delogo=x=564:y=1122:w=72:h=74"     # the baked Vids sparkle, fixed bottom-right
W, H = 720, 1280                             # the 9:16 canvas every clip is fitted onto
MUS_VOL = float(os.environ.get("MUS_VOL", "0.30"))
TAIL_FADE = float(os.environ.get("TAIL_FADE", "0.50"))
# Pace the whole reel. Generated motion often runs a touch slower than life, and the crafts used
# ~1.06x — but a pace change is a taste call on THAT reel, so this defaults to 1.0 (OFF) and is
# only applied when the reel asks for it (`speed` in identity.json, or SPEED=1.06 in the env).
# Video and audio are sped by the SAME factor, so lip-sync stays locked; atempo preserves pitch.
SPEED = float(os.environ.get("SPEED", "1.0"))
CARD_STYLE = dict(size=41, color="#FFF3D6", bg="rgba(18,16,14,0.72)")

# Dead air at a clip's ENDS only — never an internal pause, which is a performance beat and
# cutting one would butcher a spoken line. Conservative by design: it leaves TRIM_KEEP of the
# pause so a line never starts clipped, and refuses to eat more than TRIM_MAX. TRIM=0 disables.
TRIM = os.environ.get("TRIM", "1") != "0"
TRIM_DB = -35
TRIM_KEEP = 0.25
TRIM_MIN = 0.40
TRIM_MAX = 1.50


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _dur(p):
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", p]).stdout
    return float(json.loads(out)["format"]["duration"])


def _has_audio(p):
    return "audio" in _run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", p]).stdout


def _frames_differ(p, d):
    import hashlib
    seen = set()
    for t in (d * 0.1, d * 0.35, d * 0.6, d * 0.85):
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", p, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        seen.add(hashlib.md5(r.stdout).hexdigest())
    return len(seen) >= 2


def _mean_db(p):
    for ln in _run(["ffmpeg", "-hide_banner", "-i", p, "-af", "volumedetect",
                    "-f", "null", "-"]).stderr.splitlines():
        if "mean_volume:" in ln:
            return float(ln.split("mean_volume:")[1].split("dB")[0])
    return -999.0


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


def _heal(path):
    tmp = path + ".heal.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-profile:v", "high",
                    "-level", "4.0", "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", "30",
                    "-video_track_timescale", "90000", "-c:a", "aac", "-b:a", "192k",
                    "-ar", "48000", "-ac", "2", "-movflags", "+faststart", tmp],
                   check=True, capture_output=True)
    os.replace(tmp, path)


def _dead_air(clip):
    """(head, tail) seconds of dead air to cut from a clip's ends. A generated clip usually
    opens and closes on a beat of nothing; across six of them that is several seconds of the
    owner's reel spent on silence. `silencedetect` finds the pauses, and only the ones touching
    an end are eligible."""
    d = _dur(clip)
    if d <= 0:
        return 0.0, 0.0
    out = _run(["ffmpeg", "-hide_banner", "-i", clip, "-af",
                f"silencedetect=noise={TRIM_DB}dB:d=0.35", "-f", "null", "-"]).stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    if not starts or not ends:
        return 0.0, 0.0
    head = ends[0] if starts[0] <= 0.15 else 0.0
    # trailing means it RUNS TO the end — where it starts is irrelevant (a 1s tail starts 1s
    # before the end, not 0.4s; `silence_end` can also overshoot the clip length slightly)
    tail = (d - starts[-1]) if ends[-1] >= d - 0.15 else 0.0
    head = min(max(head - TRIM_KEEP, 0.0), TRIM_MAX)
    tail = min(max(tail - TRIM_KEEP, 0.0), TRIM_MAX)
    return (head if head >= TRIM_MIN else 0.0), (tail if tail >= TRIM_MIN else 0.0)


def _trims(clips, durs, xf):
    """Per-clip trims, refusing any that would leave a clip too short to survive the dissolve."""
    if not TRIM:
        return [(0.0, 0.0)] * len(clips)
    out = []
    for i, c in enumerate(clips):
        h, t = _dead_air(c)
        if durs[i] - h - t < xf + 0.8:
            h = t = 0.0
        out.append((h, t))
    return out


def _vision():
    import vision
    return vision


def style_preflight(clips):
    """The prompt saying '3-D' is not evidence — the RENDERED frame is. One ambiguous frame (an
    AR filter over a face, a phone-screen inset) must not burn a re-render, while a real
    photoreal clip is always caught."""
    if os.environ.get("STYLE_CHECK") == "0":
        print("style pre-flight skipped (STYLE_CHECK=0)")
        return True
    vis = _vision()
    bad = []
    for p in clips:
        samples = []
        for frac in (0.35, 0.7):
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{_dur(p) * frac:.2f}", "-i", p,
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True)
            if r.stdout:
                samples.append(vis.medium_of(r.stdout))
        verdict, ev = vis.medium_verdict(samples, expected="3d")
        print(f"  {os.path.basename(p)} -> {verdict} ({samples})")
        if verdict in ("drift", "wrong-medium"):
            bad.append(f"{os.path.basename(p)}: {ev}")
        elif verdict == "unknown":
            print(f"  WARNING: {os.path.basename(p)} could not be classified — check it by eye")
    if bad:
        print("STYLE DRIFT — refusing to build: " + "; ".join(bad))
        return False
    print("style pre-flight OK — no clip reads photoreal")
    return True


def _absorb(project_dir):
    """Re-audit what actually shipped and file a candidate for any layer that is missing.
    Fail-open: bookkeeping must never turn a finished build into a failed one."""
    try:
        import brain
        sys.path.insert(0, ROOT)
        r = brain.LocalBrain().absorb(project_dir)
        if r.get("ok"):
            gap = ("filed " + ", ".join(r["learned"])) if r.get("learned") else "all layers present"
            print(f"  brain absorbed: {gap}")
    except Exception as e:  # noqa: BLE001
        print(f"  brain absorb skipped ({str(e)[:110]})")


def _card_height(lines, font, width):
    """Render once off-screen and measure the ink, so placement knows the card's real height
    instead of guessing it from the line count."""
    probe = os.path.join(tempfile.mkdtemp(), "probe.png")
    caption.render(lines, probe, y=0, font=font, width=width, **CARD_STYLE)
    a = np.asarray(Image.open(probe).convert("RGBA"))[..., 3]
    rows = np.where(a.max(axis=1) > 8)[0]
    return int(rows.max() - rows.min() + 1) if len(rows) else 220


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
    if not style_preflight(clips):
        raise RuntimeError("BUILD REJECTED — style drift; requeue the offending clip")

    # ---- dead air off each end, then the xfade chain; `starts` records where each clip lands
    #      so cards can be timed against it ----
    trims = _trims(clips, durs, xf)
    eff = [durs[i] - trims[i][0] - trims[i][1] for i in range(n)]
    cut = sum(h + t for h, t in trims)
    if cut:
        print(f"  dead air: cut {cut:.2f}s across {sum(1 for h, t in trims if h or t)} clip(s)")

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
    FIT = f"scale={W}:{H}:force_original_aspect_ratio=decrease," \
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black"
    for i in range(n):
        h, t = trims[i]
        # FIT is an identity for a clip that is already 9:16, and saves the reel when a site
        # returns a square or 16:9 generation — a mixed-size join otherwise mis-frames silently.
        vf = f"{FIT},format=yuv420p,fps=30,setsar=1"
        if h or t:
            vf = f"trim=start={h:.3f}:end={eff[i] + h:.3f},setpts=PTS-STARTPTS," + vf
        fc.append(f"[{i}:v]{vf},{DELOGO}[v{i}]")
        af = "aformat=sample_rates=48000:channel_layouts=stereo"
        if _has_audio(clips[i]):
            if h or t:
                af = f"atrim=start={h:.3f}:end={eff[i] + h:.3f},asetpts=PTS-STARTPTS," + af
            else:
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
    for kind, ci, cs, ce in windows:
        lines = (spec.get(kind) or {}).get("lines") or []
        if ce <= cs or not lines:
            continue
        print(f"  card {kind}: {cs:.2f}-{ce:.2f}s (over clip {ci})")
        # the window is in REEL time; the source clip's head may have been trimmed, so the
        # sample window has to be shifted back into the clip's own untrimmed timeline
        h = trims[ci][0]
        y, _h = _place(lines, font, 560, clips[ci], max(0.0, cs - starts[ci]) + h,
                       max(0.1, ce - starts[ci]) + h)
        png = os.path.join(project_dir, f"caption_{kind}.png")
        caption.render(lines, png, y=y, font=font, **CARD_STYLE)
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
        oi = ninp
        ninp += 1
        fc.append(f"[{oi}:v]format=rgba,fade=t=in:st={cs:.3f}:d=0.4:alpha=1,"
                  f"fade=t=out:st={ce - 0.4:.3f}:d=0.4:alpha=1[capv{oi}]")
        fc.append(f"[{vmap}][capv{oi}]overlay=0:0:enable='between(t,{cs:.3f},{ce:.3f})'[vo{oi}]")
        vmap = f"vo{oi}"
        cards.append(kind)

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
        mi = ninp
        inputs += ["-i", music]
        ninp += 1
        fc.append(f"[{mi}:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                  "aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"volume={MUS_VOL},afade=t=in:st=0:d=1.4,"
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
    print(f"OK -> {out}  ({_dur(out):.2f}s, {n} clips, {', '.join(layers)})  VERIFIED")
    if absorb:
        _absorb(project_dir)
    return out


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        os.environ["STYLE_CHECK"] = "0"                 # offline: no vision gateway
        _absorb = lambda _d: None                       # never touch the real brain from a test
        d = tempfile.mkdtemp()
        pdir, cdir = os.path.join(d, "demo"), os.path.join(d, "clips", "demo")
        os.makedirs(pdir)
        os.makedirs(cdir)
        for i in range(3):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=5",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=5",
                            "-pix_fmt", "yuv420p", "-shortest",
                            os.path.join(cdir, f"clip_{i:03d}.mp4")],
                           check=True, capture_output=True)
        # clip 0 gets the dead air a generated clip really ships with: 1.2s of nothing at the
        # head, 1.0s at the tail, a spoken middle. The trim must find both and nothing else.
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:d=5",
                        "-f", "lavfi", "-i", "sine=frequency=440:d=2.8",
                        "-af", "adelay=1200|1200,apad", "-t", "5",
                        "-pix_fmt", "yuv420p", "-shortest",
                        os.path.join(cdir, "clip_000.mp4")],
                       check=True, capture_output=True)
        h, t = _dead_air(os.path.join(cdir, "clip_000.mp4"))
        assert 0.8 <= h <= 1.05, f"head dead air not found (got {h:.2f}, want ~0.95)"
        assert 0.6 <= t <= 0.85, f"tail dead air not found (got {t:.2f}, want ~0.75)"
        assert _dead_air(os.path.join(cdir, "clip_001.mp4")) == (0.0, 0.0), \
            "a clip with no dead air must be left completely alone"
        print(f"OK: dead air measured at the ends only (head {h:.2f}s, tail {t:.2f}s), "
              "a solid clip untouched")
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
        naive = 3 * 5 - 2 * 0.40
        assert _dur(out) < naive - 1.2, \
            f"dead air was not cut: reel {_dur(out):.2f}s vs untrimmed {naive:.2f}s"
        got = sorted(f for f in os.listdir(pdir) if f.startswith("caption_"))
        assert got == ["caption_cta.png", "caption_hook.png", "caption_moral.png"], got
        for f in got:
            a = np.asarray(Image.open(os.path.join(pdir, f)).convert("RGBA"))[..., 3]
            assert a.max() == 255, f"{f}: the card did not render"
            assert a[int(0.85 * 1280):].max() == 0, f"{f} leaks into the bottom 15%"
        print(f"OK: 3 clips -> a reel that plays ({_dur(out):.2f}s, dead air cut), "
              "three auto-placed cards, none in the bottom 15%")

        # ---- pace: the reel's own declared speed shortens the reel and keeps every card ------
        json.dump({"opener": "a held breath", "font": "rounded", "quote": "Kabir",
                   "structure": "bookend", "speed": 1.25},
                  open(os.path.join(pdir, "identity.json"), "w", encoding="utf-8"))
        plain = _dur(out)
        fast = build(pdir, clips_dir=cdir)
        assert abs(_dur(fast) - plain / 1.25) < 1.2, \
            f"1.25x pace did not shorten the reel: {_dur(fast):.2f}s vs {plain:.2f}s"
        assert not verify_playable(fast, _dur(fast)), "a paced reel must still play"
        got = sorted(f for f in os.listdir(pdir) if f.startswith("caption_"))
        assert got == ["caption_cta.png", "caption_hook.png", "caption_moral.png"], got
        # ...and a speed outside what atempo can do in one stage is ignored, not obeyed blindly
        assert _speed(pdir) == 1.25, "the reel's declared speed must be read"
        json.dump({"speed": 9.0}, open(os.path.join(pdir, "identity.json"), "w"))
        assert _speed(pdir) == 1.0, "an unusable speed must fall back to the native pace"
        print(f"OK: declared pace applied ({plain:.2f}s -> {_dur(fast):.2f}s), "
              "cards intact, an out-of-range speed ignored")
        raise SystemExit(0)

    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 assemble.py projects/<name>")
    build(sys.argv[1])
