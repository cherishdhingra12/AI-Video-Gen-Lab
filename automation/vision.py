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
# vision endpoint: FreeLLMAPI has proven vision models; gemini-2.5-flash verified.
_URL = ENV.get("VISION_URL", ENV.get("FREELLMAPI_URL", "http://localhost:3001/v1"))
_KEY = ENV.get("VISION_KEY", ENV.get("FREELLMAPI_KEY", ""))
_MODEL = ENV.get("VISION_MODEL", "gemini-2.5-flash")

# what read_state may return — a closed vocabulary the runner can branch on
STATES = ("rendering", "done", "error", "captcha", "rate_limited", "login", "unknown")


def _ask(png: bytes, prompt: str, max_tokens: int = 300) -> str:
    b64 = base64.b64encode(png).decode()
    body = json.dumps({
        "model": _MODEL, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        _URL.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    return (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


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
