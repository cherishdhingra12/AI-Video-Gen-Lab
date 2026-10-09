"""caption.py — PLACE and RENDER the on-screen Hindi / Hinglish cards.

The two jobs the vision needs, in one file so the assembler and the gate read the same answer:

  find_empty_band(video, t0, t1, height)  -> the y (px) of "the empty spot" in that window
  render(lines, out, y=...)               -> a transparent 720x1280 PNG of the card

Placement is the part that did not exist: every card shipped so far was hand-tuned pixels (see
projects/roz_ki_list/render_caption.py — "Was y790, grazed her chin"). This samples the frames
the card will actually sit over and puts the card where there is no face and no busy detail.

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


def _cost_map(png):
    """Per-pixel 'do not put text here'. Busy detail AND skin, because the thing a card must
    never cover is a face — and a soft-focus face is low-detail enough that edge energy alone
    would happily print a title across it."""
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
    return edge + 2.0 * skin                    # a face outweighs busy texture


def find_empty_band(video, t0, t1, height, samples=5):
    """The y (px) of the emptiest band `height` tall that a card could occupy, searching only
    the safe vertical range. None when the frames cannot be read."""
    frames = _sample(video, t0, t1, samples)
    if not frames:
        return None
    cost = np.mean([_cost_map(p) for p in frames], axis=0)
    x0, x1 = int(0.10 * W), int(0.86 * W)       # the far-right edge is off-limits too
    lo = int(0.04 * H)
    hi = int((1 - BOTTOM_CLEAR) * H) - height
    if hi <= lo:
        return max(0, min(lo, H - height))
    # ponytail: a sliding window over a mean-cost map, not a saliency model. Good enough to
    # stop a card landing on a face; upgrade to a real face detector if one ever does.
    return min(range(lo, hi + 1, max(1, height // 6)),
               key=lambda y: float(cost[y:y + height, x0:x1].mean()))


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
</style></head><body><div class="card">{body}</div></body></html>"""
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
    else:
        print("SKIP: render (Chrome not installed here)")
    print("caption --selfcheck OK")
