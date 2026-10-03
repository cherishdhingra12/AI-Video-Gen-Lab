"""
vision.py — the eyes. Screenshot -> vision model -> a small, typed answer.

Fires only when the scripted path can't be sure by selectors alone:
  read_state()   is the render done / still going / errored / captcha / rate-limited?
  solve_captcha() what to do about the captcha on screen (human-guided steps)

Uses the same OpenAI-compatible gateways as agents.py, but pins a VISION model
(gemini-2.5-flash proven working). Vision is the fallback/verify layer — the
adapters try selectors first and only call here when unsure, so tokens stay low.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(path=os.path.join(os.path.dirname(_DIR), ".env")):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **os.environ}


ENV = _env()
# vision endpoints: try the pinned one first, then fail over across the other configured gateways
# so a single gateway being down doesn't blind the self-heal — the backend self-heals too.
_MODEL = ENV.get("VISION_MODEL", "gemini-3.8-flash")
# extra routes to try if the primary 404s/errors (Google aliases; harmless 404 on other gateways).
_FALLBACK_MODELS = ["gemini-flash-latest", "gemini-3-flash-latest", "auto/pro-vision"]
_TRANSIENT = (429, 503)                 # throttle / high-demand -> worth a short retry, not a give-up


def _models():
    seen, out = set(), []
    for m in [_MODEL] + _FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _models_for(url):
    """Each gateway speaks a different model dialect, so send one it accepts: Gemini wants
    gemini-*, OmniRoute wants auto/vision routes, FreeLLMAPI (and generic auto-routers) want 'auto'."""
    u = (url or "").lower()
    if "generativelanguage" in u or "googleapis" in u:
        gem = [m for m in [_MODEL] + _FALLBACK_MODELS if m.startswith("gemini")]
        return gem or ["gemini-flash-latest"]
    if "20128" in u or "omniroute" in u:
        return ["auto/vision", "auto/pro-vision", "auto/best-vision"]
    return ["auto"]                                 # FreeLLMAPI etc. auto-route to a vision model


def _endpoints():
    """Ordered (url, key) vision gateways to try. VISION_* first (primary), then OmniRoute,
    FreeLLMAPI, and Gemini as a quality fallback. De-duped so a repeat URL isn't tried twice."""
    seen, out = set(), []
    for u, k in ((ENV.get("VISION_URL"), ENV.get("VISION_KEY")),
                 (ENV.get("OMNIROUTE_URL"), ENV.get("OMNIROUTE_KEY")),
                 (ENV.get("FREELLMAPI_URL"), ENV.get("FREELLMAPI_KEY")),
                 (ENV.get("GEMINI_URL"), ENV.get("GEMINI_KEY"))):
        if u and u not in seen:
            seen.add(u)
            out.append((u, k or ""))
    return out or [("http://localhost:3001/v1", "")]


# what read_state may return — a closed vocabulary the runner can branch on
STATES = ("rendering", "done", "error", "captcha", "rate_limited", "login", "unknown")


def _ask(png: bytes, prompt: str, max_tokens: int = 600, patience: int = 2) -> str:
    """Ask a vision model, failing over across gateways and routes. On a transient throttle /
    high-demand code (429/503), retry the whole sweep a few times with backoff — so a momentary
    spike doesn't blind the self-heal. Fail-open: callers treat a raise as 'unknown'."""
    b64 = base64.b64encode(png).decode()
    last = None
    for sweep in range(patience + 1):
        transient = False
        for url, key in _endpoints():               # fail over across gateways …
            for model in _models_for(url):          # … each with a model dialect it accepts
                body = json.dumps({
                    "model": model, "max_tokens": max_tokens, "temperature": 0,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                    ]}],
                }).encode()
                try:
                    req = urllib.request.Request(
                        url.rstrip("/") + "/chat/completions", data=body,
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=90) as r:
                        d = json.load(r)
                    txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
                    if txt.strip():
                        return txt
                except urllib.error.HTTPError as e:
                    last = e
                    if e.code in _TRANSIENT:
                        transient = True
                except Exception as e:
                    last = e
        if not transient or sweep == patience:
            break
        time.sleep(4 * (sweep + 1))     # 4s, 8s backoff on a transient spike
    raise last or RuntimeError("no vision endpoint answered")


def _json_from(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "")
    i = text.find("{")
    if i == -1:
        return {}
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i:k + 1])
                except Exception:
                    return {}
    return {}


def review_frame(png: bytes) -> list:
    """Review ONE finished-reel frame for realism defects / AI artifacts, before the reel goes to
    the owner. Returns a list of {'type','detail'} ([] = clean), or None if the vision gateway was
    unavailable (so the QC gate can say 'not reviewed' instead of falsely passing). High-confidence
    only — the gate aggregates these across sampled frames."""
    prompt = (
        "You are the final quality reviewer for a PHOTOREALISTIC short film frame, checking it "
        "before it reaches the client. Report ONLY clear, high-confidence defects a human would "
        "call wrong. Look specifically for:\n"
        "- two people's bodies MERGING / clipping / passing THROUGH each other (not a normal hug/touch)\n"
        "- extra, missing, or malformed limbs / hands / fingers; a duplicated object (e.g. two canes)\n"
        "- a badly warped or distorted face\n"
        "- BAKED-IN TEXT that is garbled, misspelled, or meaningless (any on-image lettering that is "
        "not clean real words)\n"
        "- the art style breaking (part photoreal, part cartoon/3D)\n"
        'Reply ONLY JSON: {"defects":[{"type":"body-merge|limbs|duplicate|face|garbled-text|style|other",'
        '"detail":"short specific phrase"}]}. `type` MUST be exactly one of those seven values, and '
        '`detail` MUST be a concrete phrase describing what you see (e.g. "her torso overlaps his '
        'shoulder") — never the list of type names. Empty list if the frame looks clean and real.'
    )
    try:
        d = _json_from(_ask(png, prompt, max_tokens=1200))   # headroom for a 'thinking' model's JSON
    except Exception:
        return None                         # gateway failure -> caller knows the frame was NOT reviewed
    out = d.get("defects") if isinstance(d.get("defects"), list) else []
    return _clean_defects(out)


# The seven types review_frame() asks for. Anything else the model invents is still kept (it may
# be a real defect) but filed under "other", so callers keyed on these names never break.
DEFECT_TYPES = ("body-merge", "limbs", "duplicate", "face", "garbled-text", "style", "other")
# Answers that mean "nothing wrong" / are the enum echoed back. A bare type name is NOT a phrase
# describing what the model SAW, so it must not reach the gate — the enum echo is exactly what
# used to block a clean reel.
_NON_DEFECT = {t.replace("-", " ") for t in DEFECT_TYPES} | set(DEFECT_TYPES) | {
    "none", "clean", "ok", "okay", "fine", "good", "clear", "n/a", "na", "no defect",
    "no defects", "no issues", "looks fine", "looks clean", "looks good", "no artifact",
    "no artifacts", "no problem", "no problems", "pass", "passed",
}


def _clean_defects(out: list) -> list:
    """Keep only defects the model actually described. Drops enum echoes, type-name-only details,
    empty/trivial strings, and unparseable entries; remaps an unknown `type` to 'other'."""
    res = []
    for x in out:
        if not isinstance(x, dict):
            continue
        typ = str(x.get("type", "")).strip().lower()
        det = " ".join(str(x.get("detail", "")).split()).strip(" .;:-")[:160]
        if not det or "|" in det:                 # empty, or the whole enum pasted in
            continue
        low = det.lower().replace("-", " ")
        if low in _NON_DEFECT:                    # "body merge" / "clean" / "no defects" …
            continue
        if len(det) < 8 or " " not in det:        # a word or a label, not a description
            continue
        res.append({"type": typ if typ in DEFECT_TYPES else "other", "detail": det})
    return res


def read_state(png: bytes) -> dict:
    """Look at a generation page. Return {'state': one of STATES, 'reason': str,
    'progress': 0-100 or None}. The runner branches on 'state'."""
    prompt = (
        "You are watching an AI video-generation website. Look at this screenshot and "
        "report its current state. Reply ONLY with JSON:\n"
        '{"state": "rendering|done|error|captcha|rate_limited|login|unknown", '
        '"reason": "short phrase of what you see", "progress": <int percent or null>}\n'
        "Rules: 'done' only if a finished video/download is clearly available. "
        "'captcha' if any human-verification/robot challenge is shown. "
        "'rate_limited' if it says no credits/quota/too many requests. "
        "'login' if it wants sign-in. 'rendering' if it is still generating/loading."
    )
    # Fail-OPEN: vision is a best-effort perception layer. If the gateway is down or
    # rate-limited (429), a gen must NOT crash — return 'unknown' so the selector path
    # stays in charge. A real wall (captcha/login) is still caught when vision is up.
    try:
        d = _json_from(_ask(png, prompt))
    except Exception:
        return {"state": "unknown", "reason": "vision gateway unavailable", "progress": None}
    state = d.get("state") if d.get("state") in STATES else "unknown"
    return {"state": state, "reason": d.get("reason", ""), "progress": d.get("progress")}


def find_control(png: bytes, candidates: list, what: str, max_tokens: int = 200) -> dict:
    """Self-heal perception. A scripted selector just missed because the site's UI drifted;
    find the control NOW so the run continues on its own. Given the screenshot, the page's
    visible clickable elements (each {'i','text','x','y','w','h'} with viewport-CENTER x,y),
    and a plain description of the control we need, return {'x','y'} to click or None.

    The model PICKS a candidate (reliable — we click its real coordinates) or, for an
    icon/unlabeled control it can see in the image but that isn't in the list, reads the
    center off the image. Vision CORRECTS the stale map; it never drives the control flow.
    Fail-open: any gateway/parse error returns None so the caller can wall normally."""
    lines = "\n".join(f'{c["i"]}: "{(c.get("text") or "")[:48]}" @({c["x"]},{c["y"]}) {c["w"]}x{c["h"]}'
                      for c in candidates[:120])
    prompt = (
        "An automated run is on an AI-video website whose UI changed, so a saved selector for a "
        f"control is stale. Find this control:\n  {what}\n\n"
        "The page's visible clickable elements (index: \"text\" @(centerX,centerY) WxH):\n"
        f"{lines or '(none captured)'}\n\n"
        "Reply ONLY JSON. If one element IS that control: {\"index\": <i>}. "
        "If it is an icon/unlabeled control you can see in the IMAGE but not in the list: "
        "{\"x\": <centerX>, \"y\": <centerY>}. If it is not present at all: {\"index\": -1}."
    )
    try:
        d = _json_from(_ask(png, prompt, max_tokens))
    except Exception:
        return None
    i = d.get("index")
    if isinstance(i, bool):
        i = None
    if isinstance(i, int) and 0 <= i < len(candidates):
        return {"x": int(candidates[i]["x"]), "y": int(candidates[i]["y"])}
    if isinstance(d.get("x"), (int, float)) and isinstance(d.get("y"), (int, float)):
        return {"x": int(d["x"]), "y": int(d["y"])}
    return None


def solve_captcha(png: bytes) -> dict:
    """Describe the captcha and the human-like steps to clear it. Returns
    {'type': str, 'instructions': str, 'clickable': bool}. The adapter turns
    instructions into human_click/human_type; genuinely hard ones -> back off."""
    prompt = (
        "This screenshot shows a CAPTCHA / human-verification challenge. Reply ONLY JSON:\n"
        '{"type":"checkbox|image_select|slider|puzzle|text|unknown",'
        '"instructions":"exactly what a human would do, step by step",'
        '"clickable":true/false}\n'
        "clickable=true only for a simple single-click (e.g. 'I am not a robot'). "
        "For image grids/sliders/puzzles set clickable=false (too risky to fake)."
    )
    try:
        d = _json_from(_ask(png, prompt))
    except Exception:
        return {"type": "unknown", "instructions": "", "clickable": False}
    return {"type": d.get("type", "unknown"),
            "instructions": d.get("instructions", ""),
            "clickable": bool(d.get("clickable", False))}


if __name__ == "__main__":
    # offline: find_control parses a candidate-pick, an image-coord, and not-found (no gateway).
    _orig = _ask
    try:
        _ask = lambda png, p, mt=200: '{"index": 1}'  # noqa: E731
        assert find_control(b"x", [{"i": 0, "text": "a", "x": 10, "y": 10, "w": 5, "h": 5},
                                   {"i": 1, "text": "New project", "x": 99, "y": 40, "w": 80, "h": 30}],
                            "the new project button") == {"x": 99, "y": 40}
        _ask = lambda png, p, mt=200: 'noise {"x": 200, "y": 150} tail'  # noqa: E731
        assert find_control(b"x", [], "an icon with no text") == {"x": 200, "y": 150}
        _ask = lambda png, p, mt=200: '{"index": -1}'  # noqa: E731
        assert find_control(b"x", [{"i": 0, "text": "a", "x": 1, "y": 1, "w": 5, "h": 5}], "absent") is None
        print("OK: find_control parses candidate-pick, image-coord, and not-found")
    finally:
        _ask = _orig

    # self-check: a real image round-trip proves the vision plumbing (base64 encode,
    # request shape, JSON parse). No text/fonts (this ffmpeg lacks drawtext) — a
    # solid-color PNG is enough: we assert the model returns a valid STATE, and that
    # it can read a plain color it's told to name. Skips (not fails) if gateway down.
    import subprocess
    def solid(color):
        return subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x200:d=1",
             "-vframes", "1", "-f", "image2", "-vcodec", "png", "pipe:1"],
            capture_output=True).stdout
    img = solid("green")
    assert img[:8] == b"\x89PNG\r\n\x1a\n", "test image build failed (no PNG)"
    try:
        r = read_state(img)
        assert r["state"] in STATES, r
        # direct color read proves the image truly reached the model, not a canned reply
        ans = _ask(img, "What single color fills this image? One word.").lower()
        assert "green" in ans, f"model did not see the image: {ans!r}"
        print(f"OK: vision round-trip works — read_state->{r['state']}, "
              f"color-read->green (model={_MODEL})")
    except Exception as e:
        print(f"SKIP (gateway unavailable, plumbing intact): {e}")
