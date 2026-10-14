"""publishing.py — the reel's own publishing pack: `cover.png` and `publishing.md`.

THE FOLDER IS THE DELIVERABLE. dadaji-ka-kahani (2026-10-14) is the folder the owner held up as
the template: open one directory and everything is there — the script, the shot list, the
identity, the cards, the music AND its provenance, the cut, the cover, and the words it goes out
with. Both files were made by hand there. This is that hand-work turned into code, so the next
reel gets them because the BUILD writes them (`assemble.build` calls `pack`), not because anyone
remembered to. Written for a reel built before this existed with:

    python3 automation/publishing.py projects/<name>

Everything here comes from the reel itself: the cover is a real frame OF the reel, and the copy
is written from the reel's own script and cards. Nothing is invented, and nothing is discarded —
an existing `publishing.md` is never overwritten without --force, because the owner edits it.

    python3 automation/publishing.py --selfcheck
"""
import glob
import json
import os
import subprocess
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.dirname(_DIR))       # the root, for agents.py

import numpy as np                # noqa: E402
from PIL import Image             # noqa: E402

import caption  # noqa: E402

ROOT = os.path.dirname(_DIR)

# Where the generated clips live (clips/<project>/clip_*.mp4) — the cards are added at assembly,
# so a source clip is a card-free frame of the reel. Same env override as qc.py's.
_CLIPS_ROOT = os.environ.get("AIVGL_CLIPS", os.path.join(ROOT, "clips"))

# ---- THE CONTRACT: what every reel folder carries, and who writes it -------------------------
# qc.check_reel_folder gates go_live on this list, so it is defined ONCE, here, next to the code
# that produces two of its members. `who` is documentation for the owner, not a check.
FOLDER = (
    ("script.md", "the story and its spoken lines", "agents.write_script"),
    ("shotlist.json", "the world, the cast and every shot", "agents.build_shotlist"),
    ("identity.json", "this reel's look, kept distinct from every prior reel", "agents.presentation"),
    ("captions.json", "the on-screen cards, as written", "agents.presentation"),
    ("music_raw.mp3", "the background score", "music.choose"),
    ("music_meta.json", "where the score came from + how it is placed", "music.install"),
    ("final_veo.mp4", "the cut", "assemble.build"),
    ("caption_place.json", "where each card landed in the cut (QC re-checks faces here)", "assemble.build"),
    ("music_bed.json", "the measured audio floor of the shipped cut", "assemble.build"),
    ("cover.png", "the cover — a real frame, the hook line inside the grid square", "publishing.cover"),
    ("publishing.md", "titles, caption, description, keywords, hashtags", "publishing.sheet"),
)
REQUIRED = tuple(f for f, _, _ in FOLDER)
# The cut's own name is written by whichever path built it (runner -> final.mp4), so a folder
# passing with either is correct; check_reel_folder looks for final_veo.mp4 OR final.mp4.
OPTIONAL = ("final.mp4",)

COVER_STYLE = dict(size=56, color="#FFF3D6", bg="rgba(18,16,14,0.72)", width=640)
# The Instagram profile grid shows a 1:1 CENTRE CROP of a 9:16 reel. Text outside this band is
# cropped out of the grid entirely, so the cover's line is confined to it (WAVELENGTH §9).
GRID = (0.25, 0.75)


def _final(project_dir):
    found = [p for p in (os.path.join(project_dir, f) for f in ("final_veo.mp4", "final.mp4"))
             if os.path.exists(p)]
    return max(found, key=os.path.getmtime) if found else None


def _dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _load(project_dir, name, default=None):
    try:
        with open(os.path.join(project_dir, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _free_spans(project_dir, dur):
    """The times in the SHIPPED cut with no card on screen. A cover frame must not already carry
    a card — `caption_place.json` says exactly where each one sits (it is written post-pace, in
    the shipped timeline, which is the same timeline the frame is taken from)."""
    place = _load(project_dir, "caption_place.json", {}) or {}
    busy = sorted((float(v["start"]), float(v["end"])) for v in place.values()
                  if isinstance(v, dict) and "start" in v)
    spans, cur = [], 0.0
    for s, e in busy:
        if s - cur > 0.6:
            spans.append((cur, s))
        cur = max(cur, e)
    if dur - cur > 0.6:
        spans.append((cur, dur))
    return spans or [(0.0, dur)]


def _cover_frame(project_dir, final, dur):
    """(file, t) the cover frame is taken from.

    An explicit `cover.json` wins — that is the one knob no rule can compute, and the reason it
    exists: dadaji-ka-kahani's cover was picked by eye, and its frame is **clip_000 at 3.0s**, a
    frame the CUT can never offer because the reel's own hook card is burned across that second.
    The cards are added at assembly, so a SOURCE clip carries none and every moment of it is
    eligible; the cut does not. `{"clip": N, "t": s}` names the source clip, `{"t": s}` a moment of
    the shipped cut (kept: it is the safer pick when the clips have been swept).

    With no cover.json: the middle of the shipped cut's longest card-free stretch — deterministic,
    lands on a shot rather than a transition, and cannot carry a second card. ponytail: the default
    never reaches for a source clip, because mapping a cut time back to a clip and an offset is the
    edit decision list and this module does not have it; name the pick in cover.json instead."""
    want = _load(project_dir, "cover.json", {}) or {}
    t = want.get("t")
    t = float(t) if isinstance(t, (int, float)) else None
    n = want.get("clip")
    if isinstance(n, int):
        src = os.path.join(_CLIPS_ROOT, os.path.basename(os.path.abspath(project_dir)),
                           f"clip_{n:03d}.mp4")
        if os.path.exists(src):
            return src, (t if t is not None and 0 <= t < (_dur(src) or 1.0) else 0.0)
    if t is not None and 0 <= t < dur:
        return final, t
    lo, hi = max(_free_spans(project_dir, dur), key=lambda s: s[1] - s[0])
    return final, round((lo + hi) / 2, 3)


def cover(project_dir):
    """Write `cover.png`: a REAL frame of the reel, the hook line set inside the grid square.

    A frame, never a generated image — a cover that is not in the film is a promise the film does
    not keep, and it is the one image the owner picks the reel by. Taken from the SHIPPED cut, so
    it is already the reel's own grade and already cleaned of the site's watermark; taken at a
    card-free moment, so it does not carry a second card under this one. The line is placed with
    the same face-aware placement the reel's cards use (caption.find_empty_band), narrowed to the
    square the Instagram grid keeps. -> the path, or None when there is nothing to build from."""
    final = _final(project_dir)
    lines = (_load(project_dir, "captions.json", {}) or {}).get("hook", {}).get("lines") or []
    if not final or not [ln for ln in lines if str(ln).strip()]:
        return None
    font = (_load(project_dir, "captions.json", {}) or {}).get("font") or "serif"
    dur = _dur(final) or 1.0
    src, t = _cover_frame(project_dir, final, dur)
    cdur = _dur(src) or dur
    out = os.path.join(project_dir, "cover.png")
    h = caption.card_height(lines, font, **COVER_STYLE)
    # The line must land in the grid square. find_empty_band is asked for the square only, so this
    # can only fail if the call ever stops honouring the window — and then it fails HERE, loudly,
    # instead of shipping a cover whose only line the profile grid crops off. It reads the frames
    # the card will actually sit over, so it is asked about the frame's OWN file.
    lo, hi = int(GRID[0] * caption.H), int(GRID[1] * caption.H) - h
    y = caption.find_empty_band(src, t, min(t + 0.4, cdur), h, lo_frac=GRID[0], hi_frac=GRID[1])
    y = max(lo, min(y, hi)) if y is not None else max(0, (int(GRID[1] * caption.H) - h) // 2)
    assert y >= lo and (y + h <= int(GRID[1] * caption.H) or h > hi - lo), \
        f"the cover's line left the grid square: y={y}, card {h}px, square {lo}-{hi}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src,
                    "-frames:v", "1", out], check=True)
    base = Image.open(out).convert("RGBA")
    card = os.path.join(project_dir, ".cover_card.png")
    caption.render(lines, card, y=y, font=font, **COVER_STYLE)
    # a scrim rising from the card's own row, so cream text always has ground under it whatever
    # the frame happens to be there
    mask = Image.new("L", (1, caption.H), 0)
    for yy in range(caption.H):
        if yy > y - h // 2:
            mask.putpixel((0, yy), int(170 * min(1.0, (yy - (y - h // 2)) / (caption.H * 0.25))))
    base = Image.composite(Image.new("RGBA", base.size, (16, 11, 7, 255)), base,
                                   mask.resize(base.size))
    base.alpha_composite(Image.open(card).convert("RGBA"), (0, 0))
    base.convert("RGB").save(out)
    os.remove(card)
    print(f"  cover: frame at {t:.2f}s, line at y={y} (grid square {int(GRID[0] * caption.H)}"
          f"-{int(GRID[1] * caption.H)})")
    return out


def _facts(project_dir):
    """Everything the copy is written FROM — the reel's own words, nothing else."""
    caps = _load(project_dir, "captions.json", {}) or {}
    plan = _load(project_dir, "shotlist.json", {}) or {}
    ident = _load(project_dir, "identity.json", {}) or {}
    script = ""
    sp = os.path.join(project_dir, "script.md")
    if os.path.exists(sp):
        script = open(sp, encoding="utf-8").read()
    final = _final(project_dir)
    title = plan.get("title") or os.path.basename(project_dir)
    return {
        "title": title, "seconds": _dur(final) if final else 0.0, "script": script, "cards": caps,
        "plan": json.dumps({"world": plan.get("world"), "style": plan.get("style"),
                            "music_mood": plan.get("music_mood"),
                            "moral": ident.get("moral"), "structure": ident.get("structure")},
                           ensure_ascii=False),
    }


def _fallback(facts):
    """The sheet's copy when no gateway answers. Built from the reel's own cards, so the folder
    is still complete and postable — a publish pack is not worth failing a reel over, and an
    outage must never read as a defect (qc's Unreviewed rule, one layer along)."""
    caps = facts.get("cards") or {}
    hook = " ".join((caps.get("hook") or {}).get("lines") or [])
    moral = " ".join((caps.get("moral") or {}).get("lines") or [])
    cta = " ".join((caps.get("cta") or {}).get("lines") or [])
    addr = cta or facts.get("title", "")
    return {"youtube_title": f"{hook} #Shorts".strip()[:90],
            "youtube_title_alt": f"{facts.get('title', '')} | Standard Sunrise Foundation",
            "caption": f"{hook}\n\n{moral}\n\n{addr}".strip(),
            "youtube_description": f"{hook}\n\n{moral}\n\n{addr}".strip(),
            "tags": f"{facts.get('title', '')}, hindi emotional story, hindi reels, "
                    "old age care, ngo india, standard sunrise foundation",
            "keywords": f"{facts.get('title', '')}, hindi emotional reel, hindi kahani, "
                        "motivational hindi story, respect elders, call your parents",
            "hashtags": "#StandardSunriseFoundation #HindiReels #EmotionalReels #HindiKahani "
                        "#OldAgeCare #FamilyValues #CallYourParents #NGOIndia #Reels #Shorts "
                        "#कहानी #बुज़ुर्ग #भारत"}


def _sheet_text(project_dir, copy, facts):
    """The publishing sheet, in the shape the owner's template folder uses."""
    cards = facts.get("cards") or {}
    me = _load(project_dir, "music_meta.json", {}) or {}
    bed = _load(project_dir, "music_bed.json", {}) or {}
    present = [f for f, _, _ in FOLDER if os.path.exists(os.path.join(project_dir, f))]
    track = " / ".join(p for p in (me.get("title"), me.get("artist")) if p) or "the score"
    score = [f"`music_raw.mp3` = **{track}**"
             + (f" — {me['source']}" if me.get("source") else "")
             + (f" ({me['licence']})" if me.get("licence") else "")]
    if me.get("source_url"):
        score.append(f"source: {me['source_url']}")
    if bed.get("floor") is not None:
        score.append(f"bed at level {me.get('level_start', 0)}s in, measured floor "
                     f"{bed['floor']} dB (the −46 floor gate is the failure line)")
    if not me.get("licence"):
        score.append("⚠ provenance is not recorded for this bed — it plays, and provenance is "
                     "recorded rather than gating (owner ruling 2026-10-10), but the row is missing")
    hook = " ".join((cards.get("hook") or {}).get("lines") or [])
    return f"""# {facts['title']} — publishing sheet

Deliverables in this folder: {', '.join('`%s`' % f for f in present)}.

## Titles

**YouTube (searchable Hindi):**
> {copy['youtube_title']}

**YouTube (alt, curiosity):**
> {copy['youtube_title_alt']}

**Instagram / Facebook first line** = the reel's own hook, so the profile grid and the caption agree:
> {hook}

## Caption (Instagram / Facebook)

```
{copy['caption']}
```

## YouTube description

```
{copy['youtube_description']}
```

## Keywords

**YouTube tags (comma list):**
{copy['tags']}

**Instagram / Facebook keywords (comma list):**
{copy['keywords']}

**Hashtags (IG/FB, keep to 12–15):**
{copy['hashtags']}

## Cover

`cover.png` — a real frame from the reel at {_cover_frame(project_dir, _final(project_dir), facts['seconds'])[1]}s, with the hook line
placed inside the middle half of the frame: the Instagram profile grid shows a 1:1 centre crop,
so a line near the top of a 9:16 frame would be cropped away there. (YouTube uses the reel's own
frames; a 16:9 thumbnail is a different picture and is not what this file is.) To choose a
different frame, write `{{"t": <seconds>}}` into `cover.json` and re-run
`python3 automation/publishing.py projects/{os.path.basename(project_dir)}` — or
`{{"clip": <n>, "t": <seconds>}}` to take it from a SOURCE clip (`clips/<reel>/clip_<n>.mp4`),
which carries no card and so offers every moment of the shot, not only the card-free ones.

## Score

{os.linesep.join(score)}

---

*Written by `automation/publishing.py`. Edit this file freely — re-running keeps your edits
unless you pass `--force`.*
"""


def sheet(project_dir, force=False):
    """Write `publishing.md` — the words the reel goes out with. -> the path, or None when there
    is nothing to write from. An existing file is left alone unless `force`: the owner edits it,
    and a rebuild must not silently undo that."""
    out = os.path.join(project_dir, "publishing.md")
    if os.path.exists(out) and not force:
        return None
    live = _final(project_dir)
    if not live:
        return None
    facts = _facts(project_dir)
    copy = None
    # PUBLISH_LLM=0 writes the sheet from the reel's own words — the offline suites set it, so a
    # self-check can never quietly spend a gateway call (the same idiom as STYLE_CHECK=0).
    if os.environ.get("PUBLISH_LLM", "1") != "0":
        try:
            import agents
            copy = agents.publishing_copy(facts)
        except Exception as e:  # noqa: BLE001 — an outage must never cost the folder its sheet
            print(f"  publishing: no gateway for the copy ({str(e)[:80]}) — writing from the "
                  f"reel's own words")
    if not copy:
        copy = _fallback(facts)
    with open(out, "w", encoding="utf-8") as f:
        f.write(_sheet_text(project_dir, copy, facts))
    print(f"  publishing: {os.path.basename(out)} written")
    return out


def pack(project_dir, force=False):
    """The whole pack for one reel. Both halves are independent and neither raises: a reel keeps
    its cut, its gate and its sign-off whatever happens here."""
    got = []
    for fn, name in ((cover, "cover.png"), (sheet, "publishing.md")):
        try:
            p = fn(project_dir, force=force) if fn is sheet else fn(project_dir)
            if p:
                got.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: could not be written ({str(e)[:90]}) — the reel is unaffected")
    return got


def main(argv):
    if "--selfcheck" in argv:
        return _selfcheck()
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 2
    for d in args:
        print(f"{os.path.basename(d.rstrip('/'))}:")
        pack(d, force="--force" in argv)
    return 0


def _selfcheck():
    """OFFLINE: the sheet is written from a real (fake) project folder, and every promise the
    module makes is asserted — the contract list is the one qc gates on, a reel with no cut
    writes nothing, an existing sheet is kept, and the fallback still produces a complete sheet
    with the foundation's own address (no gateway is reachable in this suite)."""
    import tempfile
    d = tempfile.mkdtemp()
    pdir = os.path.join(d, "demo")
    os.makedirs(pdir)
    assert pack(pdir) == [], "a folder with no cut must write nothing at all"

    # a real 4s cut over a PLAIN frame: the fixture is deliberately featureless so the only dark
    # pixels in the finished cover are the card scrim this module paints — a test pattern's own
    # blacks would make "is the card inside the square?" unanswerable.
    final = os.path.join(pdir, "final_veo.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=0x9a8f80:s=720x1280:d=4", "-f", "lavfi",
                    "-i", "sine=frequency=300:d=4", "-pix_fmt", "yuv420p", "-shortest", final],
                   check=True)
    json.dump({"hook": {"lines": ["अस्सी की उम्र में", "मैंने ये बातें सीखीं…"]},
               "moral": {"lines": ["बुजुर्गों की बातें बाँटने से बढ़ती हैं।"]},
               "cta": {"lines": ["Standard Sunrise Foundation — जुड़िये",
                                 "standardsunrisefoundation.org"]}, "font": "serif"},
              open(os.path.join(pdir, "captions.json"), "w"), ensure_ascii=False)
    json.dump({"hook": {"start": 0.4, "end": 1.4}, "moral": {"start": 2.0, "end": 2.8}},
              open(os.path.join(pdir, "caption_place.json"), "w"))
    json.dump({"title": "दादाजी की कहानी", "world": "a warm amber living-room",
               "style": "3-D animation"}, open(os.path.join(pdir, "shotlist.json"), "w"),
              ensure_ascii=False)
    open(os.path.join(pdir, "script.md"), "w").write("# दादाजी की कहानी\n\nIDEA: ...\n")
    json.dump({"moral": "बुजुर्गों की बातें बाँटने से बढ़ती हैं।"},
              open(os.path.join(pdir, "identity.json"), "w"), ensure_ascii=False)

    # the frame is taken from a card-free stretch: 1.4-2.0 and 2.8-4.0 are free, so the longest is
    # taken and the middle of it sampled — never a moment that already carries a card.
    spans = _free_spans(pdir, 4.0)
    assert (1.4, 2.0) in spans and (2.8, 4.0) in spans, spans
    assert (0.0, 0.4) not in spans, "a 0.4s gap is too short to trust a frame from — dropped"
    t = _cover_frame(pdir, os.path.join(pdir, "final_veo.mp4"), 4.0)[1]
    assert not any(s <= t <= e for s, e in ((0.4, 1.4), (2.0, 2.8))), \
        f"the cover frame must not land under a card: t={t}"
    assert _cover_frame(_with_json(pdir, "cover.json", {"t": 3.1}),
                        os.path.join(pdir, "final_veo.mp4"), 4.0)[1] == 3.1, \
        "cover.json must override the automatic frame"
    # ...and a SOURCE clip may be named, which is the pick the cut can never offer: the cards are
    # added at assembly, so a clip carries none. dadaji-ka-kahani's owner-picked cover is clip_000
    # at 3.0s — a second the cut spends under the reel's own hook card.
    _CR, _old_cr = _CLIPS_ROOT, _CLIPS_ROOT
    try:
        _tmp = os.path.join(tempfile.mkdtemp(), os.path.basename(pdir))
        os.makedirs(_tmp)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=0x9a8f80:s=720x1280:d=5", "-pix_fmt", "yuv420p",
                        os.path.join(_tmp, "clip_000.mp4")], check=True, capture_output=True)
        globals()["_CLIPS_ROOT"] = os.path.dirname(_tmp)
        src, ct = _cover_frame(_with_json(pdir, "cover.json", {"clip": 0, "t": 3.0}),
                               os.path.join(pdir, "final_veo.mp4"), 4.0)
        assert os.path.basename(src) == "clip_000.mp4" and ct == 3.0, (src, ct)
        # an out-of-range clip falls back to the cut rather than writing a frame nobody asked for
        src2, _ = _cover_frame(_with_json(pdir, "cover.json", {"clip": 9, "t": 3.0}),
                               os.path.join(pdir, "final_veo.mp4"), 4.0)
        assert os.path.basename(src2) == "final_veo.mp4", src2
    finally:
        globals()["_CLIPS_ROOT"] = _old_cr
        assert _CR == _old_cr

    # No gateway in this suite: `agents.publishing_copy` is pinned to raise, so the sheet below is
    # the OUTAGE path, and a selfcheck can never quietly spend quota. Both halves are exercised —
    # a copy the agent wrote, and the fallback built from the reel's own words.
    #
    # PUBLISH_LLM=0 is dropped here: that knob (which the offline SUITES set) skips the gateway call
    # outright, so with it on, the pinned stub is never reached, every sheet is the fallback, and
    # "an agent-written sheet must be what lands" can never pass. The pin below is what makes this
    # offline; the knob is redundant on top of it.
    import agents
    _real, _copy = agents.publishing_copy, None
    _knob = os.environ.pop("PUBLISH_LLM", None)
    try:
        agents.publishing_copy = lambda facts: (_ for _ in ()).throw(RuntimeError("no gateway"))
        got = pack(pdir)
        assert sorted(got) == ["cover.png", "publishing.md"], got
        im = Image.open(os.path.join(pdir, "cover.png"))
        assert im.size == (caption.W, caption.H), f"cover must be the reel canvas: {im.size}"
        ink = np.asarray(im.convert("RGB")).astype(int)
        # The card paints a dark scrim (16,11,7) under cream text, and the fixture frame is plain,
        # so the ONLY dark pixels in the cover are the card. Asserted on the scrim rather than on
        # "the brightest pixels": a generated frame's own colours are not ours to predict.
        dark = np.argwhere((ink < 40).all(axis=-1))
        assert dark.size > 5000, "the cover carries no card scrim"
        # The square is a FACT ABOUT THE APP (a 1:1 centre crop of a 9:16 frame), so the test
        # pins the numbers instead of reading GRID — otherwise widening GRID would widen the
        # assertion with it and the test could never fail.
        lo, hi = int(0.25 * caption.H), int(0.75 * caption.H)
        rows = np.unique(dark[:, 0])
        assert rows.min() >= lo - 4 and rows.max() <= hi + 4, \
            (f"the cover's card must sit inside the grid square {lo}-{hi}: its scrim spans "
             f"{rows.min()}-{rows.max()}")
        text = open(os.path.join(pdir, "publishing.md"), encoding="utf-8").read()
        for want in ("## Titles", "## Caption (Instagram / Facebook)", "## YouTube description",
                     "## Keywords", "## Cover", "## Score", agents.BRAND_URL):
            assert want in text, f"the sheet is missing {want!r}"
        assert "अस्सी की उम्र में" in text, "the sheet must carry the reel's own hook line"
        assert "BED" not in text or "music_raw.mp3" in text, "the score line must name the bed"
        # a sheet the AGENT wrote is what normally ships: prove _sheet_text renders that too
        os.remove(os.path.join(pdir, "publishing.md"))
        _copy = dict(_fallback(_facts(pdir)), youtube_title="AGENT-WROTE-THIS")
        agents.publishing_copy = lambda facts: _copy
        sheet(pdir)
        assert "AGENT-WROTE-THIS" in open(os.path.join(pdir, "publishing.md"),
                                          encoding="utf-8").read(), \
            "an agent-written sheet must be what lands, not the fallback"

        # the owner edits it; a rebuild must not silently undo that
        open(os.path.join(pdir, "publishing.md"), "w").write("my edits\n")
        assert pack(pdir) == ["cover.png"], "an existing publishing.md must be kept, not replaced"
        assert open(os.path.join(pdir, "publishing.md")).read() == "my edits\n"
        pack(pdir, force=True)
        assert "my edits" not in open(os.path.join(pdir, "publishing.md")).read(), \
            "--force must replace it"
    finally:
        agents.publishing_copy = _real
        if _knob is not None:
            os.environ["PUBLISH_LLM"] = _knob

    # every file the contract names must be produced by SOMEONE'S code, or the gate would be
    # asking for something nothing writes
    assert "cover.png" in REQUIRED and "publishing.md" in REQUIRED and "final.mp4" in OPTIONAL
    assert len(set(REQUIRED)) == len(REQUIRED), "the contract has a duplicate"
    print("OK: the pack — a real frame for the cover, the hook inside the grid square, and a "
          "complete sheet that survives an outage and an owner's edits")
    print("publishing --selfcheck OK")
    return 0


def _with_json(pdir, name, obj):
    json.dump(obj, open(os.path.join(pdir, name), "w"))
    return pdir


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
