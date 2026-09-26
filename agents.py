"""
Agents 1 & 2 — the LLM half of the pipeline. Runs at kickoff (local gateways).

  write_script(idea)      -> screenplay text        (agent 1)
  build_shotlist(script)  -> keyframe-chain dict     (agent 2)

LLM access = OpenAI-compatible gateways with automatic fallback. Order is set
by LLM_ORDER in .env (default OmniRoute -> FreeLLMAPI, as requested). OmniRoute's
premium routers were hanging at build time, so the fail-fast timeout drops to
FreeLLMAPI (reliable) — flip LLM_ORDER=freellmapi,omniroute to skip the wait.

No third-party deps: stdlib urllib only.
"""

import json
import os
import re
import urllib.request

_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env(path=os.path.join(_DIR, ".env")):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **os.environ}  # real env wins


ENV = _load_env()

# name -> (url, key, default model, timeout seconds)
_GATEWAYS = {
    "omniroute": (ENV.get("OMNIROUTE_URL"), ENV.get("OMNIROUTE_KEY"),
                  ENV.get("OMNIROUTE_MODEL", "auto/smart"), 20),
    "freellmapi": (ENV.get("FREELLMAPI_URL"), ENV.get("FREELLMAPI_KEY"),
                   ENV.get("FREELLMAPI_MODEL", "auto"), 120),
}
_ORDER = [x.strip() for x in ENV.get("LLM_ORDER", "omniroute,freellmapi").split(",") if x.strip()]


def chat(messages, max_tokens=4000, temperature=0.8):
    """Try each gateway in order; return (text, "gateway:model") from the first
    that returns non-empty content. Raises if all fail."""
    errors = []
    for name in _ORDER:
        url, key, model, timeout = _GATEWAYS.get(name, (None, None, None, 30))
        if not url or not key:
            continue
        try:
            body = json.dumps({
                "model": model, "messages": messages,
                "max_tokens": max_tokens, "temperature": temperature, "stream": False,
            }).encode()
            req = urllib.request.Request(
                url.rstrip("/") + "/chat/completions", data=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            content = (d.get("choices") or [{}])[0].get("message", {}).get("content")
            if content and content.strip():
                return content, f"{name}:{d.get('model', model)}"
            errors.append(f"{name}: empty content")
        except Exception as e:  # noqa: BLE001 - any failure -> next gateway
            errors.append(f"{name}: {e}")
    raise RuntimeError("all LLM gateways failed: " + " | ".join(errors))


def write_script(idea: str, seconds: int = 15) -> str:
    """Agent 1: idea -> vivid, shootable short-video script."""
    system = (
        "You are an award-winning short-film director and screenwriter. "
        "You turn a one-line idea into a vivid, concrete, shootable micro-script "
        "for an AI-generated video. You write what the audience SEES and HEARS, "
        "never vague abstractions."
    )
    user = (
        f"IDEA: {idea}\n\n"
        f"Write a script for a ~{seconds}-second cinematic video. Include:\n"
        "- LOGLINE: one sentence.\n"
        "- VISUAL STYLE: palette, film stock/look, mood (one paragraph).\n"
        "- BEATS: a numbered beat-by-beat of the visual action and any narration/audio.\n"
        "Keep it tight, concrete, and directable."
    )
    text, used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2000, temperature=0.9,
    )
    return text


_SCHEMA = """{
  "title": "short title",
  "world": "the LOCKED universal anchor: setting/place, time of day, weather, season, overall lighting & color atmosphere, and the recurring subject's fixed description (who/what, exact clothes/colors/features). This never changes across clips.",
  "style": "ONE film-look/quality phrase every clip prompt ends with (film stock, lens, grade, 'cinematic, highly detailed')",
  "aspect": "9:16",
  "clips": [
    {"idx": 0, "prompt": "a COMPLETE text-to-video prompt for ONE ~5s shot: the action + camera move in THIS shot. It will be sent to a frontier video site as-is, with 'world' prepended and 'style' appended automatically.", "narration": "optional VO line or empty string"}
  ]
}"""


def build_shotlist(script: str, aspect: str = "9:16", clip_seconds: int = 5) -> dict:
    """Agent 2: script -> a list of standalone per-clip text-to-video prompts, all
    sharing one locked 'world' (the consistency anchor). Each clip is generated
    independently on a frontier site, then stitched. No keyframe interpolation —
    the sites do their own motion; continuity comes from the shared world + style."""
    system = (
        "You are a master cinematographer and AI-video director. You convert a "
        "script into a shot list of standalone text-to-video prompts for frontier "
        "video websites (Veo, Kling, Hailuo). Output ONLY valid JSON, no prose."
    )
    user = (
        f"SCRIPT:\n{script}\n\n"
        f"Produce a shot plan as JSON with EXACTLY this shape:\n{_SCHEMA}\n\n"
        "RULES:\n"
        "- Write 'world' FIRST and make it rich and specific — it is prepended to "
        "every clip prompt, so no clip can drift or hallucinate a new place/character.\n"
        "- 3 to 6 clips. Each 'prompt' is a COMPLETE, self-contained ~"
        f"{clip_seconds}s shot description (action + camera move) that reads well on "
        "its own. Do NOT restate 'world' inside it — it is added automatically.\n"
        "- Keep the recurring subject identical shot to shot. Use gentle, continuous "
        "camera moves (slow push-in, drift, pan) so consecutive clips cut together smoothly.\n"
        f"- aspect '{aspect}' (vertical/portrait for reels).\n"
        "- Return JSON ONLY."
    )
    text, used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=4000, temperature=0.7,
    )
    return _extract_json(text)


def clip_prompts(shot: dict) -> list:
    """Materialize the final per-clip prompts: world + shot + style. This is what
    gets pasted into each site. Kept here so the runner/pipeline stay dumb."""
    world = (shot.get("world") or "").strip()
    style = (shot.get("style") or "").strip()
    out = []
    for c in sorted(shot.get("clips", []), key=lambda x: x.get("idx", 0)):
        parts = [world, c["prompt"].strip(), style]
        out.append(", ".join(p for p in parts if p))
    return out


def _extract_json(text: str) -> dict:
    """LLMs wrap JSON in fences or reasoning; grab the first balanced object."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in LLM output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON in LLM output")


if __name__ == "__main__":
    # self-check: real end-to-end of the LLM half on a tiny idea.
    s = write_script("a paper boat's journey down a rain gutter to the sea", seconds=12)
    print("SCRIPT (first 200 chars):\n", s[:200], "\n---")
    plan = build_shotlist(s)
    assert plan.get("clips"), "missing clips"
    assert plan.get("world"), "missing world anchor (consistency lock)"
    for c in plan["clips"]:
        assert c.get("prompt"), "clip missing prompt"
    prompts = clip_prompts(plan)
    assert len(prompts) == len(plan["clips"]), "clip_prompts count mismatch"
    assert plan["world"][:15].lower() in prompts[0].lower(), \
        "world anchor must be prepended to each clip prompt"
    print(f"OK: '{plan.get('title')}' — {len(prompts)} clips, aspect {plan.get('aspect')}")
    print("   clip[0]:", prompts[0][:120], "...")
