"""
Agents — the LLM half of the pipeline. Runs at kickoff (local gateways).

  write_script(idea)              -> screenplay text          (agent 1)
  build_shotlist(script)          -> keyframe-chain dict      (agent 2)
  presentation(script, shot, ...) -> identity + caption card  (agent 3)

LLM access = OpenAI-compatible gateways with automatic fallback. Order is set
by LLM_ORDER in .env (default OmniRoute -> FreeLLMAPI, as requested). OmniRoute's
premium routers were hanging at build time, so the fail-fast timeout drops to
FreeLLMAPI (reliable) — flip LLM_ORDER=freellmapi,omniroute to skip the wait.

No third-party deps: stdlib urllib only.
"""

import json
import os
import re
import sys
import time
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
# The timeout must clear the model's slowest realistic answer, not its average: OmniRoute's free
# tier returns a full presentation in ~22s and has run to ~48s, so the old 20s timed out on a
# model that was working — and the retry loop then burned 3 x 20s discovering it.
_GATEWAYS = {
    "omniroute": (ENV.get("OMNIROUTE_URL"), ENV.get("OMNIROUTE_KEY"),
                  ENV.get("OMNIROUTE_MODEL", "auto/smart"),
                  int(ENV.get("OMNIROUTE_TIMEOUT", "90"))),
    "freellmapi": (ENV.get("FREELLMAPI_URL"), ENV.get("FREELLMAPI_KEY"),
                   ENV.get("FREELLMAPI_MODEL", "auto"),
                   int(ENV.get("FREELLMAPI_TIMEOUT", "120"))),
}
_ORDER = [x.strip() for x in ENV.get("LLM_ORDER", "omniroute,freellmapi").split(",") if x.strip()]

_WAVELENGTH_MD = os.path.join(_DIR, "WAVELENGTH.md")


def _wavelength() -> str:
    """The house wavelength (WAVELENGTH.md), read at CALL time so an edit lands on the very next
    prompt. It lives in a file rather than inside these prompt strings for one reason: a model
    or gateway swap must not be able to lose it. "" when absent, so the pipeline still runs."""
    try:
        with open(_WAVELENGTH_MD) as f:
            return f.read().strip()
    except OSError:
        return ""


def _system(role: str) -> str:
    """A role prompt with the house wavelength in front of it, so no agent can forget it."""
    wl = _wavelength()
    return f"{wl}\n\n---\n\n{role}" if wl else role


# A free router answers a 429 "Too Many Requests" most days. Falling through to the next gateway
# on the first one meant a single rate-limit could cost the whole run — the route was fine, it
# just needed a second. Retries are per gateway: waiting out a busy free tier beats abandoning it.
_TRIES = int(ENV.get("LLM_RETRIES", "3"))
_TRANSIENT = ("429", "Too Many Requests", "500", "502", "503", "504", "timed out",
              "Connection reset", "Remote end closed", "empty content")


def _retryable(err) -> bool:
    """A busy or broken-looking server is worth another go; a 401/403/404 never is."""
    return any(k in str(err) for k in _TRANSIENT)


def chat(messages, max_tokens=4000, temperature=0.8):
    """Try each gateway in order; return (text, "gateway:model") from the first
    that returns non-empty content. Raises if all fail."""
    errors = []
    for name in _ORDER:
        url, key, model, timeout = _GATEWAYS.get(name, (None, None, None, 30))
        if not url or not key:
            continue
        for attempt in range(_TRIES):
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
                raise ValueError("empty content")
            except Exception as e:  # noqa: BLE001 - any failure -> retry, then next gateway
                errors.append(f"{name}[{attempt + 1}]: {e}")
                if attempt + 1 < _TRIES and _retryable(e):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    raise RuntimeError("all LLM gateways failed: " + " | ".join(errors))


def write_script(idea: str, seconds: int = 15) -> str:
    """Agent 1: idea -> vivid, shootable short-video script."""
    system = _system(
        "You are an award-winning short-film director and screenwriter. "
        "You turn a one-line idea into a vivid, concrete, shootable micro-script "
        "for an AI-generated video. You write what the audience SEES and HEARS, "
        "never vague abstractions."
    )
    user = (
        f"IDEA: {idea}\n\n"
        f"Write a script for a ~{seconds}-second cinematic video. Include:\n"
        "- LOGLINE: one sentence.\n"
        "- VISUAL STYLE: palette, 3-D animation look, mood (one paragraph).\n"
        "- BEATS: a numbered beat-by-beat of the visual action and, for each beat, the exact "
        "Hindi line a character speaks on camera — or a note that the beat is silent.\n"
        "Keep it tight, concrete, and directable."
    )
    text, used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2000, temperature=0.9,
    )
    return text


_SCHEMA = """{
  "title": "short title",
  "slug": "a short lowercase ASCII folder name for this reel — letters, digits and dashes only, e.g. 'the-tailors-thread'. A Hindi title MUST be transliterated here; this names the reel's folder on disk, so it must not be empty or a placeholder.",
  "world": "the LOCKED universal anchor: setting/place, time of day, weather, season, overall lighting & color atmosphere, and the recurring subject's fixed description (who/what, exact clothes/colors/features). This never changes across clips.",
  "style": "the 3-D animation anchor from WAVELENGTH.md §1 — the phrase every clip prompt opens AND closes with (rounded shapes, soft global illumination, cinematic lighting, shallow depth of field)",
  "music_mood": "search words for the background score this reel needs, in plain English — e.g. 'mournful solo bansuri evening raga' or 'playful indian folk dholak festive'. Used to search for a track when nothing in the library fits.",
  "music_want": "one of exactly: bright | playful | warm | neutral | tender | mournful | sombre — where this reel's score sits on the brightness scale. A comic reel is bright; a reel about loss is mournful.",
  "aspect": "9:16",
  "clips": [
    {"idx": 0, "prompt": "a COMPLETE text-to-video prompt for ONE ~5s shot: the action + camera move in THIS shot. It will be sent to a frontier video site as-is, with 'world' prepended and 'style' appended automatically.", "dialogue": "the Hindi line a character SPEAKS ON CAMERA in this shot, or an empty string for a silent beat. This is written into the generation prompt so the clip's own audio carries it — there is no narration track added later."}
  ]
}"""


def build_shotlist(script: str, aspect: str = "9:16", clip_seconds: int = 5) -> dict:
    """Agent 2: script -> a list of standalone per-clip text-to-video prompts, all
    sharing one locked 'world' (the consistency anchor). Each clip is generated
    independently on a frontier site, then stitched. No keyframe interpolation —
    the sites do their own motion; continuity comes from the shared world + style."""
    system = _system(
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
        "- The subject's description must ALSO name what they do NOT have — end it with "
        "the negatives explicitly (e.g. 'no glasses, no hat, no jewellery') unless an "
        "accessory is deliberate and then state it in 'world'. A lock that lists only "
        "what IS there cannot stop what gets ADDED: the generator invented round glasses "
        "on one clip of one reel and on no other, and every prompt still dutifully named "
        "the same man.\n"
        "- Keep the recurring subject identical shot to shot. Use gentle, continuous "
        "camera moves (slow push-in, drift, pan) so consecutive clips cut together smoothly.\n"
        "- 'music_mood' + 'music_want' describe the SCORE the reel needs, and must agree "
        "with its feeling: a solemn raga under a comic beat is the wrong film. Write the "
        "mood as words someone would actually search for.\n"
        f"- aspect '{aspect}' (vertical/portrait for reels).\n"
        "- Return JSON ONLY."
    )
    text, used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=4000, temperature=0.7,
    )
    return _extract_json(text)


PROMPT_CHARS = 4000          # keep a typed prompt inside every site's input comfortably

# A caption card renders into a 560px box at 41px (caption.FONTS/caption.render); past ~34 a
# "line" is no longer one glance. CARD_CHARS is what the prompt ASKS for; CARD_HARD is what the
# contract REJECTS. They are two numbers on purpose: no model counts Devanagari characters
# reliably, and failing the whole art direction (identity AND every card) over +2 chars meant a
# repair loop the model could not win. 34-44 wraps to a second visual line and still reads on a
# phone; past 44 it is a paragraph the card cannot hold.
CARD_CHARS = 34
CARD_HARD = 44
CARD_KINDS = ("hook", "moral", "cta")
_DEVA = re.compile(r"[ऀ-ॿ]")


def _is_animated(style: str) -> bool:
    s = (style or "").lower()
    return any(k in s for k in ("3d", "animation", "animated", "cg", "cartoon", "toon",
                                "illustrat", "puppet", "stylized", "stylised"))


def _anti(style: str) -> str:
    """The negations that actually hold the look. A style block that only says what the
    style IS still lets an early shot fall through to photoreal (aawaaz clip 0 rendered
    photoreal while clips 1-3 came back 3-D — same prompt, model drift)."""
    if _is_animated(style):
        return ("This is a computer-generated animation, NOT photorealistic, NOT live action, "
                "NOT real footage, NOT a 2-D drawing: no real skin pores, no camera grain of a "
                "real lens, no photographic realism — every frame, every clip, one single "
                "animated look.")
    return ("Stay inside this exact look in every frame and every clip: no style drift, no "
            "mixed rendering.")


def _char_lock(shot: dict) -> str:
    """A short reminder of WHO must not change, so a face/costume can't wander between
    clips even when the shot description names the character only once."""
    world = (shot.get("world") or "")
    names = re.findall(r"\b([A-Z]{3,})\b", world)          # the ALL-CAPS character tags
    if not names:
        return ""
    return ("Characters are identical in every clip and to the descriptions above — same "
            + ", ".join(names) + ": same face, same hair, same glasses, same clothes, no "
            "swapping or redesigning between shots.")


def clip_prompts(shot: dict) -> list:
    """Materialize the final per-clip prompts. This is what gets pasted into each site.
    Kept here so the runner/pipeline stay dumb.

    ORDER IS THE FIX. The old builder was world + shot + style, style LAST — and that is
    exactly how aawaaz shipped a photoreal opener in front of 3-D clips: the model latched
    onto the concrete world description first and only saw the style constraint at the very
    end. Now the style LEADS (short, first sentence), the world+shot follow, and the full
    style block is RESTATED verbatim at the end with the anti-photoreal negation attached,
    plus an explicit character lock. Long shots trim the lead back off rather than overflow.
    """
    world = (shot.get("world") or "").strip()
    style = (shot.get("style") or "").strip()
    if not style:
        return [", ".join(p for p in (world, c["prompt"].strip()) if p)
                for c in sorted(shot.get("clips", []), key=lambda x: x.get("idx", 0))]

    anti = _anti(style)
    lock = _char_lock(shot)
    head = "STYLE for every frame of this clip, no exceptions: " + style.split(".")[0].strip() + "."
    tail = ", ".join(p for p in (style, anti, lock) if p)
    out = []
    for c in sorted(shot.get("clips", []), key=lambda x: x.get("idx", 0)):
        says = (c.get("dialogue") or "").strip()
        if says:
            # Narration must come OUT of the clip (WAVELENGTH.md §3), so the line is written into
            # the generation prompt for a character to SPEAK — never laid over at assembly.
            says = ('the character speaks this line aloud in Hindi, on camera, with natural '
                    f'lip-sync — no narrator, no voiceover: "{says}"')
        body = ", ".join(p for p in (world, c["prompt"].strip(), says) if p)
        p = ", ".join(p for p in (head, body, tail) if p)
        if len(p) > PROMPT_CHARS and head:            # trim the lead first — style stays twice
            p = ", ".join(x for x in (body, tail) if x)
        if len(p) > PROMPT_CHARS:                      # then the redundant tail sentences
            p = (p[:PROMPT_CHARS - 1].rsplit(" ", 1)[0] + ".")
        out.append(p)
    return out


_PRESENTATION_SCHEMA = """{
  "identity": {
    "reel": "the project slug",
    "brand": "standardsunrisefoundation_ngo",
    "opener": "how THIS reel opens — the specific first-image device (e.g. 'object-discovery: hands open a drawer of handwritten lists'), never a genre word",
    "visual_style": "the look, and it must read as ANIMATION (contain the words '3-D animation')",
    "transitions": "the transition language for THIS reel, chosen to differ from the prior reels listed below",
    "caption_style": "the card treatment — shape, tilt, ink, any device",
    "font": "the Devanagari face and weight",
    "narration": "who speaks and how — in-scene, spoken by a character (there is no narrator track)",
    "structure": "the shape, e.g. 'object-reveal -> montage -> gut-punch -> silent reunion'",
    "ending": "how it lands",
    "moral": "the moral in one line, English is fine here (this field is documentation, not on-screen text)",
    "motif": "the single recurring object or image"
  },
  "captions": {
    "font": "rounded or serif",
    "hook":  {"lines": ["line one", "line two"]},
    "moral": {"lines": ["line one", "line two"]},
    "cta":   {"lines": ["line one"]}
  }
}"""


def presentation(script: str, shot: dict, prior: list = None) -> dict:
    """Agent 3: the reel's IDENTITY and its on-screen caption cards.

    These are the two things the owner had to hand-write per reel (projects/*/identity.json and
    captions.json), which is why the engine could never finish a reel on its own. Both come out
    of one reading of the script, so they are one call.

    `prior` is the list of identities already shipped: WAVELENGTH.md §5 forbids repeating an
    editing pattern, so the new reel has to be TOLD what the old ones did in order to differ."""
    seen = ""
    if prior:
        rows = []
        for p in prior[:8]:
            if isinstance(p, dict):
                rows.append("; ".join(f"{k}={p.get(k)}" for k in
                                      ("opener", "transitions", "caption_style", "font",
                                       "structure", "ending") if p.get(k)))
        if rows:
            seen = ("\n\nALREADY USED by earlier reels in this house — this reel must NOT repeat "
                    "any of these devices; pick a different one:\n- " + "\n- ".join(rows))
    system = _system(
        "You are a reel art-director. You decide how a finished reel LOOKS on screen: its "
        "opening device, its transition language, its typography, and the three caption cards. "
        "Output ONLY valid JSON, no prose."
    )
    user = (
        f"SCRIPT:\n{script}\n\nSHOT PLAN:\n{json.dumps({k: v for k, v in shot.items() if k != 'clips'}, ensure_ascii=False)}\n"
        f"{seen}\n\nProduce JSON with EXACTLY this shape:\n{_PRESENTATION_SCHEMA}\n\nRULES:\n"
        "- The caption cards are read on a phone, muted, in about three seconds each. Every "
        f"line must be under {CARD_CHARS} characters. One or two lines per card, never three.\n"
        "- The hook and the moral cards are written in DEVANAGARI. The CTA may be Hinglish or "
        "the brand name in Roman. Never English-only — see the audience rules above.\n"
        "- 'hook' is the opening card (a question, a provocation, or the first line of the "
        "story). 'moral' is the turn — it lands the point. 'cta' is the last card.\n"
        "- 'identity.visual_style' must state that this is 3-D animation. This is not optional.\n"
        "- The identity must be DISTINCT from the prior reels listed above.\n"
        "- Return JSON ONLY."
    )
    text, _used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=2000, temperature=0.9,
    )
    obj = _extract_json(text)
    caps = obj.get("captions")
    if isinstance(caps, dict):
        caps["font"] = _font_class(caps.get("font"))
    _presentation_selfcheck(obj)
    return obj


# WAVELENGTH.md §4 hands the model the FACE list (Baloo 2, Rozha One, …), so a model asked for a
# "face" answers with the face. The renderer only knows the class (caption.FONTS), and every face
# in §4 belongs to one — so map it instead of failing a whole reel over wording. An unrecognised
# answer falls back to the house default rather than costing the reel; the craft gate still sees
# the face the identity claims.
_FACE_CLASS = (("baloo", "rounded"), ("mukta", "rounded"), ("kohinoor", "rounded"),
               ("rozha", "serif"), ("tiro", "serif"), ("sangam", "serif"))


def _font_class(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in ("rounded", "serif"):
        return s
    for face, cls in _FACE_CLASS:
        if face in s:
            return cls
    return "rounded"


def _presentation_selfcheck(obj: dict) -> bool:
    """OFFLINE contract for presentation(). A card that overflows, an empty card, or a reel that
    quietly declares itself photoreal would each reach the owner's screen — so they are asserted
    here, where they cost nothing, rather than discovered after a render."""
    ident = obj.get("identity")
    assert isinstance(ident, dict), "presentation: no identity object"
    for k in ("opener", "visual_style", "transitions", "caption_style", "font", "narration",
              "structure", "ending", "moral", "motif"):
        assert (ident.get(k) or "").strip(), f"identity is missing '{k}'"
    assert _is_animated(ident["visual_style"]), (
        "identity.visual_style must declare animation (WAVELENGTH.md §1) — a photoreal declaration "
        "would make the craft gate check this reel against the wrong medium")
    caps = obj.get("captions")
    assert isinstance(caps, dict), "presentation: no captions object"
    assert caps.get("font") in ("rounded", "serif"), \
        f"captions.font must be a known face, got {caps.get('font')!r}"
    for kind in CARD_KINDS:
        card = caps.get(kind)
        assert isinstance(card, dict), f"captions is missing '{kind}'"
        lines = [ln.strip() for ln in (card.get("lines") or []) if ln and ln.strip()]
        assert 1 <= len(lines) <= 2, f"'{kind}' must have 1-2 lines, got {len(lines)}"
        for ln in lines:
            assert len(ln) <= CARD_HARD, \
                f"'{kind}' line is {len(ln)} chars (> {CARD_HARD}, asked for {CARD_CHARS}) — " \
                f"the card cannot hold it: {ln!r}"
    # WAVELENGTH.md: Devanagari carries the hook and the moral. The CTA may be Roman.
    for kind in ("hook", "moral"):
        lines = " ".join(caps[kind]["lines"])
        assert _DEVA.search(lines), \
            f"'{kind}' must be in Devanagari (WAVELENGTH.md audience rules), got {lines!r}"
    return True


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


def _prompt_selfcheck(shot):
    """OFFLINE contract for clip_prompts — the fix for a shipped reel, so it must be testable
    without spending an LLM token. The defect was a photoreal scene 1 in front of 3-D scenes 2-4
    built from the SAME prompt, because the style block was appended LAST. These assertions are
    the regression test for exactly that; running them only under --with-network (where they
    live in __main__ today) means CI never sees them."""
    style = (shot.get("style") or "").strip()
    world = (shot.get("world") or "").strip()
    prompts = clip_prompts(shot)
    assert len(prompts) == len(shot["clips"]), "clip_prompts count mismatch"
    if not style:                                   # a plan with no style block still builds
        return True
    for i, pr in enumerate(prompts):
        assert pr.lower().startswith("style for every frame"), \
            f"clip {i}: the style anchor must LEAD — appended last, an early shot drifts photoreal"
        assert world[:15].lower() in pr.lower(), f"clip {i}: world anchor must be present"
        assert shot["clips"][i]["prompt"][:20].lower() in pr.lower(), \
            f"clip {i}: the shot description must be present"
        assert "NOT photorealistic" in pr, \
            f"clip {i}: the anti-photoreal negation must be attached"
        # narration lives IN the clip (WAVELENGTH.md §3): a shot's spoken line must reach the
        # generation prompt, because there is no assembly-time track to carry it instead
        _says = (shot["clips"][i].get("dialogue") or "").strip()
        if _says:
            assert _says in pr, \
                f"clip {i}: the spoken line must reach the prompt — narration has to come out of the clip"
        # The full block must appear VERBATIM **after the shot description** — that is what
        # "restated" means, and it is position-relative rather than a fixed tail slice (the
        # anti-photoreal + character-lock sentences legitimately follow it, so the block can
        # land well before the midpoint and still be the tail copy).
        _low = pr.lower()
        assert style.lower() in _low, f"clip {i}: the full style block is missing"
        _after_shot = _low.rfind(style.lower()) > _low.find(
            shot["clips"][i]["prompt"][:20].lower())
        assert _after_shot, \
            f"clip {i}: the full style block must be RESTATED after the shot, not only in the lead"
    # character lock: every ALL-CAPS character tag in the world reaches every prompt
    tags = re.findall(r"\b([A-Z]{3,})\b", world)
    for t in tags:
        assert all(t in pr for pr in prompts), \
            f"character lock missing '{t}' from some prompt — identity can drift clip to clip"
    assert all(len(pr) <= PROMPT_CHARS for pr in prompts), \
        "a prompt may not overflow a site's input"
    return True


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        # OFFLINE half: pure prompt-assembly logic, no gateway, no quota.
        _shot = {"title": "t", "aspect": "9:16",
                 "world": ("An old-age home at night. BABUJI — a frail old man in a mustard "
                           "kurta. SNEHA — a young caretaker in teal."),
                 "style": ("Soft stylized 3D animation in a high-end animated-feature look: "
                           "rounded shapes, cinematic lighting. NOT photorealistic."),
                 "clips": [{"idx": 0, "prompt": "A phone records him humming in the dark.",
                            "dialogue": "यह आवाज़ किसकी है?"},
                           {"idx": 1, "prompt": "She lowers the phone and smiles."},
                           {"idx": 2, "prompt": "The phone plays his voice back to residents.",
                            "dialogue": "सुनो, यह उनकी आवाज़ है।"}]}
        _prompt_selfcheck(_shot)
        assert "यह आवाज़ किसकी है?" in clip_prompts(_shot)[0], \
            "a shot's spoken line must be written into its generation prompt"
        assert "यह आवाज़ किसकी है?" not in clip_prompts(_shot)[1], \
            "a silent beat must not acquire a line it never had"
        # a plan with no style block must still produce one prompt per clip, uncrashed
        _nostyle = dict(_shot, style="")
        assert len(clip_prompts(_nostyle)) == 3, "a styleless plan must still build every clip"
        # a very long plan must be trimmed to fit, never silently overflow
        _big = dict(_shot, world=_shot["world"] + " " + ("extra detail " * 400))
        assert all(len(pr) <= PROMPT_CHARS for pr in clip_prompts(_big)), \
            "an oversized plan must be trimmed to PROMPT_CHARS"
        print("OK: clip_prompts leads with the style anchor, restates it, negates photorealism, "
              "locks every character, writes each shot's spoken line INTO the prompt "
              "(narration comes out of the clip), and fits the input")
        print("OK: a styleless plan still builds; an oversized plan is trimmed, not dropped")

        # --- presentation(): the identity + the caption cards the engine must author itself ---
        def _ident(ident_over=None, caps=None):
            d = {"reel": "t", "brand": "standardsunrisefoundation_ngo",
                 "opener": "object-discovery: a hand opens a rusted tin",
                 "visual_style": "soft stylized 3-D animation, warm key light",
                 "transitions": "hard cuts on the beat", "caption_style": "chalk on slate",
                 "font": "Baloo 2 rounded", "narration": "in-scene, spoken by BABUJI",
                 "structure": "tin -> memory -> turn -> quiet ending", "ending": "silent hold",
                 "moral": "the tin was never about the coins", "motif": "a rusted tin"}
            d.update(ident_over or {})
            c = {"font": "rounded",
                 "hook": {"lines": ["वो डिब्बा क्यों छुपा रखा था?"]},
                 "moral": {"lines": ["डिब्बे में सिक्के नहीं,", "यादें थीं।"]},
                 "cta": {"lines": ["Standard Sunrise Foundation"]}}
            c.update(caps or {})
            return {"identity": d, "captions": c}

        _BAD_CAP = {"font": "rounded", "hook": {"lines": ["ठीक"]},
                    "moral": {"lines": ["ठीक"]}, "cta": {"lines": ["ठीक"]}}
        _presentation_selfcheck(_ident())
        print("OK: presentation() identity + cards pass their contract")
        # each of these is a real way a bad card reaches the owner's screen
        _bads = [
            ("overflowing line", _ident(caps=dict(_BAD_CAP,
                                                  hook={"lines": ["य" * (CARD_HARD + 1)]}))),
            ("empty card", _ident(caps=dict(_BAD_CAP, hook={"lines": []}))),
            ("three-line card", _ident(caps=dict(_BAD_CAP,
                                                 moral={"lines": ["एक", "दो", "तीन"]}))),
            ("English-only hook", _ident(caps=dict(_BAD_CAP, hook={"lines": ["Why the tin?"]}))),
            ("unknown font", _ident(caps=dict(_BAD_CAP, font="comic-sans"))),
            ("photoreal declaration", _ident(ident_over={"visual_style":
                                                         "photorealistic warm documentary"})),
            ("missing identity field", _ident(ident_over={"motif": ""})),
        ]
        for why, bad in _bads:
            try:
                _presentation_selfcheck(bad)
            except AssertionError:
                continue
            raise AssertionError(f"presentation must reject a {why} — it would reach the screen")
        # A line a shade over the ASK is not a defect: it wraps to a second visual line and still
        # reads, and the model cannot count Devanagari glyphs anyway. Only a paragraph is rejected.
        _soft = _ident(caps=dict(_BAD_CAP, hook={"lines": ["य" * (CARD_CHARS + 2)]}))
        _presentation_selfcheck(_soft)
        print(f"OK: a card line up to {CARD_HARD} chars is tolerated (asked for {CARD_CHARS})")

        # A model handed WAVELENGTH §4's face list answers with the FACE, which is the same answer
        # to the question the schema asked. presentation() maps it to the class the renderer knows;
        # _presentation_selfcheck stays strict, so only the mapping (not the contract) is lenient.
        for said, want in (("Baloo 2", "rounded"), ("Rozha One", "serif"), ("Mukta Vaani", "rounded"),
                           ("Tiro Devanagari Hindi", "serif"), ("rounded", "rounded"),
                           ("serif", "serif"), ("comic-sans", "rounded"), (None, "rounded")):
            assert _font_class(said) == want, f"_font_class({said!r}) should be {want!r}"
        _mapped = _ident(caps=dict(_BAD_CAP, font="Baloo 2"))
        _mapped["captions"]["font"] = _font_class("Baloo 2")
        _presentation_selfcheck(_mapped)      # the mapped answer must clear the strict contract
        print("OK: a face name written where a font class belongs is mapped, not fatal")
        print("OK: presentation() rejects overflow, an empty card, an English-only hook, "
              "a photoreal identity, and an unknown font")
        raise SystemExit(0)

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
    _prompt_selfcheck(plan)
    print(f"OK: '{plan.get('title')}' — {len(prompts)} clips, aspect {plan.get('aspect')}")
    print("   clip[0]:", prompts[0][:120], "...")
