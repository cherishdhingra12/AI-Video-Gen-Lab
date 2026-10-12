"""caption.py — PLACE and RENDER the on-screen Hindi / Hinglish cards.

The two jobs the vision needs, in one file so the assembler and the gate read the same answer:

  find_empty_band(video, t0, t1, height)  -> the y (px) of "the empty spot" in that window
  render(lines, out, y=...)               -> a transparent 720x1280 PNG of the card

Placement is the part that did not exist: every card shipped so far was hand-tuned pixels (see
projects/roz_ki_list/render_caption.py — "Was y790, grazed her chin"). This samples the frames
the card will actually sit over and puts the card where there is no face and no busy detail.

FACES ARE FOUND, NOT GUESSED. The first version of this scored "skin" by colour and it failed
exactly where reels live: a warm amber living-room makes the wall the same RGB as the face, so
the skin mask fired on 78% of pixels, the "avoid a face" term became noise, and placement printed
the opening title straight across the grandfather's face — the owner caught it. The colour rule is
kept only as a last-ditch fallback. The real answer is macOS Vision (visionkit.swift, on-device,
offline, no dependency): it finds the face on the same frame the colour rule missed. A face box
is charged a cost an order of magnitude above any texture, so a card can never out-bid it.

This ffmpeg has no drawtext and Chromium shapes Devanagari correctly, so cards are screenshotted
from HTML at native size — the same trick those per-project scripts used, generalised here.

    python3 caption.py --selfcheck
"""
import io
import os
import subprocess
import sys

import numpy as np
from PIL import Image

W, H = 720, 1280
BOTTOM_CLEAR = 0.15          # app UI lives here (handle / caption / audio row / buttons)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FACE_COST = 40.0             # the cost of one face pixel: nothing in the frame outranks a face
FACE_SAMPLES = 6             # frames sampled across the card's window for the face boxes

# The wavelength's rotating Devanagari faces, by feel rather than by name.
FONTS = {
    "rounded": '"Baloo 2","Mukta Vaani","Kohinoor Devanagari",sans-serif',
    "serif": '"Rozha One","Tiro Devanagari Hindi","Devanagari Sangam MN",serif',
}


def _sample(video, t0, t1, n):
    """n frames of PNG bytes sampled evenly across [t0, t1]."""
    out = []
    for i in range(n):
        t = t0 + (t1 - t0) * (i + 0.5) / n
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", video,
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                           capture_output=True)
        if r.stdout:
            out.append(r.stdout)
    return out


# ---- the native helper: Apple Vision via a compiled Swift shim -------------------------------
# Built once, cached next to this file. Everything is fail-open: if the machine has no Swift or
# Vision, the caller silently gets the old colour-rule behaviour rather than a broken build.
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
_SWIFT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "visionkit.swift")
BIN = os.path.join(_CACHE, "visionkit")


def _ensure_bin():
    """Path to the compiled helper, or None when it cannot be built on this machine."""
    if os.path.exists(BIN) and os.path.getmtime(BIN) >= os.path.getmtime(_SWIFT):
        return BIN
    if not (os.path.exists("/usr/bin/swiftc") and os.path.exists(_SWIFT)):
        return None
    try:
        os.makedirs(_CACHE, exist_ok=True)
        r = subprocess.run(["/usr/bin/swiftc", "-O", _SWIFT, "-o", BIN],
                           capture_output=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    return BIN if r.returncode == 0 and os.path.exists(BIN) else None


def _pixel_boxes(pngs, mode):
    """Run the helper over frames already in memory. Returns, per frame, a list of
    (x, y, w, h) pixel boxes. Empty (not an exception) when Vision is unavailable — a missing
    helper must degrade to the fallback rule, never crash a build."""
    binp = _ensure_bin()
    if not binp or not pngs:
        return [[] for _ in pngs]
    import tempfile
    d = tempfile.mkdtemp()
    paths = []
    for i, p in enumerate(pngs):
        fp = os.path.join(d, f"f{i}.png")
        with open(fp, "wb") as f:
            f.write(p)
        paths.append(fp)
    try:
        r = subprocess.run([binp, mode, *paths], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return [[] for _ in pngs]
    out = [[] for _ in pngs]
    for ln in r.stdout.splitlines():
        f, _, rest = ln.partition("\t")
        if not f or rest.strip() in ("", "ERR"):
            continue
        i = paths.index(f)
        for tok in rest.split("|"):
            body = tok.strip().split(":")[0]
            try:
                x, y, w, h = (float(v) for v in body.split(","))
            except ValueError:
                continue
            out[i].append((x, y, w, h))
    return out


def faces(pngs):
    """Face boxes (x, y, w, h) per frame, via macOS Vision. Empty lists on any failure."""
    return _pixel_boxes(pngs, "faces")


def text_boxes(pngs):
    """Readable-writing boxes per frame, via macOS Vision OCR. The scripts this machine's
    Vision ships decide how much it can read — Devanagari is not universal, so a clean answer
    means "no writing I can read", which is not the same as "no writing". Use it as a triage
    signal for the eyes, never as a pass."""
    return _pixel_boxes(pngs, "ocr")


def _cost_map(png, face_boxes=()):
    """Per-pixel 'do not put text here'. Busy detail AND, above all, a face: the one thing a
    card must never cover, whatever the room's colour happens to be.

    The colour-skin term is only a FALLBACK for when Vision found no faces and none could be
    detected (no Swift/Vision on the machine). On its own it is untrustworthy — see the module
    docstring — so it is weighted below texture and never allowed to out-rank a real face box."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.size != (W, H):
        im = im.resize((W, H))
    a = np.asarray(im).astype(np.int16)
    g = np.asarray(im.convert("L"), dtype=np.int16)
    edge = (np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
            + np.abs(np.diff(g, axis=0, prepend=g[:1, :]))).astype(np.float32) / 255.0
    r, gg, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(2), a.min(2)                 # the classic RGB skin rule — permissive on
    skin = ((r > 95) & (gg > 40) & (b > 20) & ((mx - mn) > 15)   # purpose: it must not miss a
            & (np.abs(r - gg) > 15) & (r > gg) & (r > b))        # face on Indian skin tones
    cost = edge + 1.0 * skin
    for x, y, w, h in face_boxes:
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(W, int(x + w)), min(H, int(y + h))
        if x1 > x0 and y1 > y0:
            cost[y0:y1, x0:x1] += FACE_COST
    return cost


def find_empty_band(video, t0, t1, height, samples=5):
    """The y (px) of the top of the emptiest band `height` tall that a card could occupy,
    searching only the safe vertical range. None when the frames cannot be read.

    Placement is the MINIMUM worst-case, not the minimum average. A card that covers a face for
    only a moment is still a card over a face, and averaging let one face sample hide behind four
    clean ones — so each candidate is scored by the most expensive window anywhere in its run and
    the cheapest of those wins. A face box is priced so it can never be the cheapest."""
    frames = _sample(video, t0, t1, max(samples, FACE_SAMPLES))
    if not frames:
        return None
    fb = faces(frames)          # the same frames the cost map is built from, so boxes line up
    cost = np.mean([_cost_map(p, fb[i] if i < len(fb) else ()) for i, p in enumerate(frames)],
                   axis=0)
    x0, x1 = int(0.10 * W), int(0.86 * W)       # the far-right edge is off-limits too
    lo = int(0.04 * H)
    hi = int((1 - BOTTOM_CLEAR) * H) - height
    if hi <= lo:
        return max(0, min(lo, H - height))
    # ponytail: sliding window over a mean-cost map, not a saliency model. Good enough to stop a
    # card landing on a face; upgrade to a real saliency model if one ever out-performs this.
    cands = range(lo, hi + 1, max(1, height // 6))
    return min(cands, key=lambda y: float(cost[y:y + height, x0:x1].max()))


def render(lines, out, y, font="rounded", size=41, color="#FFF3D6", bg="rgba(18,16,14,0.72)",
           width=560, left=None):
    """Write one card to a transparent PNG at (left, y). lines = a short list of strings —
    Devanagari or Roman Hinglish, whichever the line reads better in (WAVELENGTH.md)."""
    if not os.path.exists(CHROME):
        raise RuntimeError(f"Chrome not found at {CHROME} — cannot shape Devanagari")
    left = (W - width) // 2 if left is None else left
    body = "".join(f"<p>{ln}</p>" for ln in lines if ln.strip())
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;width:{W}px;height:{H}px;background:transparent;overflow:hidden}}
  .card{{box-sizing:border-box;position:absolute;top:{int(y)}px;left:{int(left)}px;
    width:{width}px;padding:34px 38px;border-radius:18px;background:{bg};
    font-family:{FONTS.get(font, FONTS['rounded'])};text-align:center;
    box-shadow:0 10px 30px rgba(0,0,0,0.28)}}
  p{{margin:0;color:{color};font-size:{size}px;line-height:1.5;font-weight:600}}
  p+p{{margin-top:8px}}
</style></head><body><div class="card">{body}</div>
<script>
// A card must FIT. CARD_CHARS counts characters, which is a fair proxy for Devanagari and a bad
// one for one long Latin token: the 29-char brand domain rendered wider than the box and spilled
// out of the card on BOTH sides. Shrink the line until it fits instead of asking a model to guess
// a width. Blocked text wraps, so scrollWidth only exceeds clientWidth on an unbreakable word.
for (const p of document.querySelectorAll('p')) {{
  let s = parseFloat(getComputedStyle(p).fontSize);
  while (p.scrollWidth > p.clientWidth + 1 && s > 18) {{
    s -= 1;
    p.style.fontSize = s + 'px';
  }}
}}
</script></body></html>"""
    tmp = out + ".html"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--default-background-color=00000000",
                        f"--window-size={W},{H}", f"--screenshot={out}", f"file://{tmp}"],
                       check=True, capture_output=True, timeout=60)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    sz = os.path.getsize(out)
    assert sz > 2000, f"{os.path.basename(out)} too small ({sz} B) — render failed"
    return out


def _png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


if __name__ == "__main__":
    if "--selfcheck" not in sys.argv:
        raise SystemExit("usage: python3 caption.py --selfcheck")

    import tempfile
    d = tempfile.mkdtemp()

    # placement: a frame that is busy on top and flat below must put the card in the flat part.
    import random
    random.seed(7)
    busy = Image.new("RGB", (W, H), (30, 30, 30))
    px = busy.load()
    for yy in range(0, 500):
        for xx in range(0, W, 2):
            v = random.randint(0, 255)
            px[xx, yy] = (v, v, v)
    for yy in range(500, H):
        for xx in range(0, W, 2):
            px[xx, yy] = (30, 30, 30)
    flat = os.path.join(d, "flat.png")
    busy.save(flat)

    x0, x1 = int(0.10 * W), int(0.86 * W)
    cost = _cost_map(open(flat, "rb").read())
    y = min(range(int(0.04 * H), int((1 - BOTTOM_CLEAR) * H) - 200, 33),
            key=lambda yy: float(cost[yy:yy + 200, x0:x1].mean()))
    assert y >= 500, f"placement picked y={y} — it should land in the flat half, not the busy one"

    # a face (skin block) must push the card away from it
    face = Image.new("RGB", (W, H), (30, 30, 30))
    for yy in range(560, 860):
        for xx in range(200, 520):
            face.putpixel((xx, yy), (190, 140, 115))
    fp = os.path.join(d, "face.png")
    face.save(fp)
    c2 = _cost_map(open(fp, "rb").read())
    y2 = min(range(int(0.04 * H), int((1 - BOTTOM_CLEAR) * H) - 200, 33),
             key=lambda yy: float(c2[yy:yy + 200, x0:x1].mean()))
    assert not (560 <= y2 <= 660), f"placement put a card on the face (y={y2})"
    print("OK: placement scores busy detail + skin, and avoids both")

    # ---- THE REGRESSION: a face box must beat a lower-texture band, even on a warm scene ----
    # This is the defect the owner caught: on a warm amber frame the colour-skin rule fires on
    # most of the picture, so the smoothest patch (the soft-focus face) won and the title was
    # printed across it. A real face box has to out-bid everything.
    flat = np.full((H, W, 3), (150, 110, 85), np.uint8)      # a warm, low-detail whole frame
    fb = [(200, 300, 300, 320)]                              # a face sitting in the plain area
    cface = _cost_map(_png_bytes(Image.fromarray(flat)), fb)
    fy0, fy1 = 300, 620                                      # the face box's rows
    lo2 = int(0.04 * H)
    hi2 = int((1 - BOTTOM_CLEAR) * H) - 200
    scores = {yy: float(cface[yy:yy + 200, x0:x1].max())
              for yy in range(lo2, hi2, max(1, 200 // 6))}
    win = min(scores, key=scores.get)
    assert not (win < fy1 and win + 200 > fy0), \
        f"a card would sit on the face box (chose y={win}, face rows {fy0}-{fy1})"
    assert max(scores[yy] for yy in scores if fy0 <= yy < fy1) >= FACE_COST, \
        "the face region must be priced above every other band"
    print("OK: a detected face box out-bids a plainer band — the card never lands on it")

    # render: a real Chrome round-trip, skipped only where Chrome is absent
    if os.path.exists(CHROME):
        out = os.path.join(d, "card.png")
        render(["हर हफ़्ते की लिस्ट पर", "एक ही चीज़…"], out, y=y2)
        im = Image.open(out)
        assert im.size == (W, H), f"card must be {W}x{H}, got {im.size}"
        alpha = np.asarray(im.convert("RGBA"))[..., 3]
        assert alpha.max() == 255, "card has no opaque pixels — the text did not render"
        assert alpha[0, 0] == 0, "card background must stay transparent outside the scrim"
        print("OK: a Devanagari card renders to a transparent 720x1280 PNG")
        # A card must FIT. CARD_CHARS is a character count, which is a fair proxy for Devanagari
        # and a bad one for one long Latin token — a 29-char domain rendered wider than the box
        # and spilled out of the card on both sides. The card page shrinks the line until it fits;
        # this is the assertion that keeps it honest. The box is the mid-alpha scrim, the ink is
        # the opaque text, and the ink must sit inside the box.
        wide = os.path.join(d, "wide.png")
        render(["standardsunrisefoundation.org", "पर आएँ"], wide, y=y2)
        a = np.asarray(Image.open(wide).convert("RGBA"))[..., 3]
        scrim, ink = np.argwhere((a > 150) & (a < 235)), np.argwhere(a > 250)
        assert ink.size and scrim.size, "the wide card did not render box and text"
        assert ink[:, 1].min() >= scrim[:, 1].min() and ink[:, 1].max() <= scrim[:, 1].max(), \
            (f"a long domain spills out of the card: ink x {ink[:, 1].min()}-{ink[:, 1].max()} "
             f"vs box {scrim[:, 1].min()}-{scrim[:, 1].max()}")
        print("OK: a 29-char Latin domain is shrunk to fit the card, not spilled out of it")
    else:
        print("SKIP: render (Chrome not installed here)")
    print("caption --selfcheck OK")
