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
import sys
import time
import urllib.error
import urllib.request

import routerhealth  # same dir: remembers which gateway is walled, so a dead route is skipped

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
    # A PINNED model beats the router's own pick. 'auto' on a router may answer with a DIFFERENT
    # vision model on every call, so the same unchanged frame gets different verdicts — measured:
    # six identical calls on one frame returned 3d x3 / photoreal x3, and the style gate then
    # rejected a finished reel on a coin flip. Pin VISION_MODEL in .env; 'auto' is the last
    # resort, never the default. (FreeLLMAPI does serve gemini-3.8-flash, so the fast local
    # route is kept AND the model is fixed.)
    if _MODEL and _MODEL != "auto":
        # The pin is the ONLY model asked for here. 'auto' must NOT be a silent fallback: measured
        # 2026-10-10, a rate-limited pin fell through to 'auto', which returned 6,900 characters of
        # reasoning prose and no JSON — compare_character then reported "vision gateway
        # unavailable" while the gateway was answering fine. A fallback the caller cannot parse is
        # worse than none, because it hides the real cause. A busy pin fails over to the NEXT
        # ENDPOINT (Gemini direct), which is a real capacity pool, not a different model.
        return [_MODEL]
    return ["auto"]                                 # nothing pinned: let the router choose


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


# WHO ANSWERED. A verdict from an unnamed model cannot be argued with: when the gate says "clip 2
# is photoreal", the first question is always which model said so, and until this existed the
# answer was nowhere on the record. Append-only, read by qc.run to stamp the verdict.
# ponytail: a module global, not a return value — this is a single-threaded CLI, and threading a
# model name through every compare_*/medium_of signature would touch every caller for one string.
ANSWERED = []


def _ask(png: bytes, prompt: str, max_tokens: int = 600, patience: int = 2) -> str:
    """Ask a vision model, failing over across gateways and routes. On a transient throttle /
    high-demand code (429/503), retry the whole sweep a few times with backoff — so a momentary
    spike doesn't blind the self-heal. Fail-open: callers treat a raise as 'unknown'.

    A route the LAST call found walled is skipped, not re-discovered: the 90s timeout and the
    sweep backoff make a dead gateway the most expensive thing in the run, and the craft gate
    makes dozens of vision calls per reel (routerhealth). If everything is cooling, everything is
    tried anyway — a memory of failure must never be the reason a check cannot run."""
    b64 = base64.b64encode(png).decode()
    last = None
    for sweep in range(patience + 1):
        transient = False
        ends = _endpoints()
        if all(routerhealth.cooling(u)[0] for u, _ in ends):
            routerhealth.clear()                    # nothing left to skip: last resort
        for url, key in ends:                       # fail over across gateways …
            until, why = routerhealth.cooling(url)
            if until:
                last = last or RuntimeError(f"{url} cooling {int(until - time.time())}s: {why}")
                continue
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
                        # the model the SERVER reports, not the one we asked for: a gateway is free
                        # to route 'auto', and the difference is exactly what a failover hides
                        ANSWERED.append({"url": url, "asked": model,
                                         "model": d.get("model") or model})
                        routerhealth.note_success(url)
                        return txt
                    routerhealth.note_failure(url, "empty content")
                except urllib.error.HTTPError as e:
                    last = e
                    routerhealth.note_failure(url, f"HTTP {e.code}")
                    if e.code in _TRANSIENT:
                        transient = True
                except Exception as e:
                    last = e
                    routerhealth.note_failure(url, e)
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
        "You are the final quality reviewer for a short film frame, checking it before it "
        "reaches the client. The film may be photoreal, 3D-animated, or 2D — judge every defect "
        "AGAINST the look this frame is meant to have, and never report 'this is not "
        "photoreal' as a defect: rendering animation is a deliberate creative choice, not a "
        "flaw. Report ONLY clear, high-confidence defects a human would call wrong. Look "
        "specifically for:\n"
        "- two people's bodies MERGING / clipping / passing THROUGH each other (not a normal hug/touch)\n"
        "- extra, missing, or malformed limbs / hands / fingers; a duplicated object (e.g. two canes)\n"
        "- a badly warped or distorted face\n"
        "- BAKED-IN TEXT that is garbled, misspelled, or meaningless (any on-image lettering that is "
        "not clean real words)\n"
        "- the art style breaking WITHIN this frame (part photoreal, part cartoon/3D — i.e. "
        "two looks fighting in one frame), or breaking against the rest of the film\n"
        'Reply ONLY JSON: {"defects":[{"type":"body-merge|limbs|duplicate|face|garbled-text|style|other",'
        '"detail":"short specific phrase"}]}. `type` MUST be exactly one of those seven values, and '
        '`detail` MUST be a concrete phrase describing what you see (e.g. "her torso overlaps his '
        'shoulder") — never the list of type names. If the frame looks clean and real, return an '
        'EMPTY list — never answer with the words of these instructions (e.g. never write '
        '"short specific phrase" itself).' 
    )
    try:
        d = _json_from(_ask(png, prompt, max_tokens=1200))   # headroom for a 'thinking' model's JSON
    except Exception:
        return None                         # gateway failure -> caller knows the frame was NOT reviewed
    out = d.get("defects") if isinstance(d.get("defects"), list) else []
    return _clean_defects(out)


def compare_character(png: bytes):
    """Review a CONTACT SHEET (one panel per scene) of the film's recurring character and say
    whether it is the same person throughout. Returns {'consistent': bool, 'detail': str}, or
    None if the gateway was unavailable — so the gate can say 'not reviewed' instead of passing.

    The sheet is ONE image on purpose: the gateway takes a single image per call, and a montage
    is exactly the question 'same person in every panel?', which no single frame can answer."""
    prompt = (
        "This image is a contact sheet: several panels, each a frame from a DIFFERENT scene of "
        "the same short film. The film has one recurring main character. Decide whether that "
        "character is the SAME PERSON in every panel — same face shape, same age, same hair, "
        "same skin tone, same distinctive features (glasses, beard, jewellery).\n"
        "Clothing and lighting may legitimately change between scenes; that is NOT an "
        "inconsistency, and neither is a change of pose or camera angle.\n"
        "The film may be 3-D animation: judge against that look, and never report 'this is "
        "animation' or 'this is not photoreal' as a defect.\n"
        "Ignore any panel that shows no person at all.\n"
        'Reply ONLY JSON: {"same": true|false, "detail": "one short specific phrase naming what '
        'differs, or an empty string when they are the same"}. `detail` must describe what you '
        "SEE (e.g. \"the beard is gone by the third panel\") — never restate these instructions."
    )
    try:
        d = _json_from(_ask(png, prompt, max_tokens=600))
    except Exception:
        return None
    if not isinstance(d.get("same"), bool):
        return None                                  # unparseable -> treat as not reviewed
    detail = str(d.get("detail") or "").strip()
    if detail.lower() in _NON_DEFECT or detail.lower() in _ECHO:
        detail = ""                                  # enum echo is not a description
    return {"consistent": d["same"], "detail": detail}


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

# The model sometimes answers with OUR instruction wording instead of what it saw (it held a
# reel as "other @ 3.1s: short specific phrase" before this existed). Exact phrases only —
# a real description like "the sign shows a specific phrase of gibberish" must survive.
_ECHO = {
    "short specific phrase", "a short specific phrase", "specific phrase",
    "a concrete phrase", "concrete phrase", "phrase describing what you see",
    "detail must be a concrete phrase", "detail", "description of what you see",
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
        if low in _ECHO:                          # our own instruction wording, not a finding
            continue
        if len(det) < 8 or " " not in det:        # a word or a label, not a description
            continue
        if typ == "style" and _is_style_note(det):
            continue                               # names the film's medium; not a finding
        res.append({"type": typ if typ in DEFECT_TYPES else "other", "detail": det})
    return res


def medium_of(png: bytes):
    """Classify ONE frame's medium: 'photoreal' | '3d' | '2d' | 'mixed', or None.

    This is the ONLY place that knows how to ask the question safely. Two traps, both hit in
    practice: (1) a reasoning model burns its whole budget on a 'Thinking Process' preamble
    before the JSON, so max_tokens must be generous or the parse silently returns {}; (2) ask
    with an option list and the model will hand the list straight back — so the answer is
    validated against the vocabulary and a non-member counts as 'no answer' (None), never as a
    pass. Callers treat None as UNKNOWN and say so out loud rather than assuming clean.
    """
    prompt = (
        "You are an art director checking the look of one shot of an animated short. "
        "Judge ONLY what the image itself shows. medium must be exactly ONE word that you "
        "choose from this set: photoreal / 3d / 2d / mixed. Use photoreal for real footage "
        "with real skin and a real lens; 3d for CG animation like a Pixar feature; 2d for a "
        "flat cartoon; mixed if the frame itself contains both. Return STRICT JSON as "
        '{"medium": "<your one word>"}. Fill it with your own observation of this image — '
        "never copy these instructions back as the value."
    )
    try:
        d = _json_from(_ask(png, prompt, max_tokens=1600, patience=1))
    except Exception:
        return None                                   # gateway down -> unknown, not clean
    m = d.get("medium") if isinstance(d, dict) else None
    return m if m in ("photoreal", "3d", "2d", "mixed") else None


# A "style defect" that is really just the film BEING animated is a false positive that HELD a
# finished reel: aawaaz shipped 3-D on purpose and the reviewer's photoreal framing returned
# "3D animated CGI style instead of photorealism". These phrases only name WHAT the film is.
_STYLE_NOT_DEFECT = ("instead of photoreal", "not photoreal", "rather than photoreal",
                     "rather than real", "not real footage", "cgi style", "animated cgi",
                     "cartoon style", "is animated", "is a 3d", "style is animated",
                     "in animation", "as animation")
# …but only when the frame is UNIFORM. A detail that reports two looks DISAGREEING is a real
# finding even when it names an animated medium ("her face photoreal while his is 3D animated"),
# so disagreement markers veto the exemption. Without this the filter would hide actual drift —
# caught by the check below, not assumed.
_STYLE_DISAGREES = ("while", "whereas", " but ", "part ", "mixed", "vs", "versus", "against",
                    "breaks", "shifts", "switch", "inconsistent", "conflict", "half", "blend")


def _is_style_note(detail: str) -> bool:
    """True when a `style` detail merely names the film's medium instead of reporting that the
    medium BREAKS. Spared only when no disagreement marker is present, so genuine drift — two
    looks fighting inside one frame or against the rest of the film — still counts."""
    d = (detail or "").lower()
    if any(m in d for m in _STYLE_DISAGREES):
        return False
    return any(p in d for p in _STYLE_NOT_DEFECT)


def medium_verdict(samples, expected=None):
    """Decide whether a set of per-frame media classifications means the look DRIFTED.

    samples: medium strings from several frames of one clip/reel ('3d', 'photoreal', '2d',
              'mixed'), plus None for any frame the classifier could not answer.
    expected: the medium the project actually ASKED for (e.g. '3d'), or None when unknown.

    Two questions were being conflated here, and conflating them HELD six shipped reels. This
    library's older reels (apne, haath_thaame, roz_ki_list...) are PHOTOREALISTIC by design, and
    a "photoreal frame is proof of drift" rule declares every one of them inconsistent with
    itself. So:
      * 'drift'         -> the frames disagree WITH EACH OTHER. Always a defect, whatever the
                           intended medium. (A photoreal frame among 3-D ones is the exact
                           defect this whole check exists for.)
      * 'wrong-medium'  -> they agree, but not on what was requested (only when `expected`).
      * 'uniform'       -> they agree (and match `expected`, if given).
      * 'unknown'       -> nothing could be classified. Never silently clean.
    'mixed' is ambiguous by nature — an AR filter over a face, a phone-screen inset — so one
    such frame among consistent ones is spared, while 2+ count as corroboration of drift.
    """
    known = [s for s in samples if s]
    if not known:
        return "unknown", "no frame could be classified"
    distinct = {s for s in known if s != "mixed"}
    if len(distinct) > 1:
        return "drift", "frames disagree: " + "/".join(sorted(distinct))
    if known.count("mixed") >= 2:
        return "drift", "%d frames read mixed" % known.count("mixed")
    agreed = (sorted(distinct)[0] if distinct else "mixed")
    if expected and agreed != expected:
        return "wrong-medium", f"reel is {agreed}, identity asks for {expected}"
    return "uniform", agreed


# ---- the look of one clip against another: the question medium_of cannot answer ----------------
# medium_of asks each frame for an absolute label out of four (photoreal/3d/2d/mixed), and the
# style gate then compares the labels. Measured 2026-10-12 on dadi-ki-dhansuchi: it answered '3d'
# for all five clips — including one whose frame at full size has skin pores and individual hair
# strands — so medium_verdict called the reel uniform and the gate passed a reel whose clips
# visibly differ in realism. The bucket is too coarse for the boundary that actually matters
# ("painterly 3-D" vs "near-photoreal 3-D" both round to '3d'), and a second gateway agreeing on
# a coarse label is still coarse. Two frames shown SIDE BY SIDE is a far easier question, and it
# is the one the gate is really asking: "are these the same look?" Measured on the same five
# clips, the paired form flagged both mismatches and passed the control (two moments of one clip).
#
# compare_style takes ONE image: the two frames composed side by side. The composing is the
# caller's job (qc._pair / assemble's sampler), because only the caller knows what to compare.

_STYLE_PROMPT = (
    "These are two frames from the SAME short film, side by side (left, right). Answer two "
    "separate questions. (1) art style: are the two frames rendered in the SAME technique and "
    "the same level of realism/detail, or is one clearly more photoreal, or more cartoonish, "
    "than the other? (2) person: is the human in both frames the same individual — same face "
    "shape, eyes, age, features? Clothing, lighting and pose may legitimately differ, and a "
    "film may be 3-D animation: never report 'this is animation' as a difference. "
    'Reply ONLY JSON: {"same_style": true|false, "style_detail": "short phrase", '
    '"same_person": true|false, "person_detail": "short phrase"}'
)


def compare_style(png_pair: bytes):
    """Two frames composed side by side -> {'same_style', 'style_detail', 'same_person',
    'person_detail'}, or None when the gateway could not answer (never a silent pass)."""
    try:
        d = _json_from(_ask(png_pair, _STYLE_PROMPT, max_tokens=1200))
    except Exception:
        return None
    if not isinstance(d.get("same_style"), bool):
        return None                                   # unparseable -> not reviewed
    out = {"same_style": d["same_style"]}
    for k in ("style_detail", "person_detail"):
        v = str(d.get(k) or "").strip()
        out[k] = "" if v.lower() in _NON_DEFECT or v.lower() in _ECHO else v[:160]
    out["same_person"] = d.get("same_person") if isinstance(d.get("same_person"), bool) else None
    return out


def _pair(a: bytes, b: bytes, height: int = 560) -> bytes:
    """Two PNG frames side by side, scaled to a common height, with a seam between them."""
    import io
    from PIL import Image
    ia, ib = Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB")
    ia = ia.resize((max(1, int(ia.width * height / ia.height)), height))
    ib = ib.resize((max(1, int(ib.width * height / ib.height)), height))
    c = Image.new("RGB", (ia.width + ib.width + 10, height), (0, 0, 0))
    c.paste(ia, (0, 0))
    c.paste(ib, (ia.width + 10, 0))
    buf = io.BytesIO()
    c.save(buf, "PNG")
    return buf.getvalue()


def style_scan(frames, anchor=0):
    """One frame per clip, in reel order (PNG bytes). Compare every other clip to the ANCHOR —
    clip 0, the first one finished, which is what locks the site and the character for the reel —
    and return:

        {'flagged': [indices], 'whole_reel': bool, 'detail': str,
         'people': {index: bool}, 'person_detail': str}

    or None when the gateway could not answer. The SAME comparison answers two questions —
    'same look?' and 'same person?' — because they are what one clip-vs-clip call can see, and one
    call per clip is what the budget allows. (`whole_reel`/`detail` are the look; `people` is the
    character, keyed by clip index, so a drifting face can be named as precisely as a drifting
    style.)

    `whole_reel` is the honest guard: when MORE than half the clips differ from the anchor the
    anchor itself is the odd one out, and re-shooting the majority would be re-shooting the reel.
    That is a decision for a human, not a requeue, so those come back flagged as whole-reel and
    the caller must not auto-regenerate. It also protects the grandfathered photoreal reels: a
    photoreal reel whose scenes read 'different' to each other is never silently re-shot."""
    if len(frames) < 2:
        return {"flagged": [], "whole_reel": False, "detail": "", "people": {},
                "person_detail": ""}
    a = frames[anchor]
    flagged, detail, people, pdetail = [], "", {}, ""
    for i, f in enumerate(frames):
        if i == anchor:
            continue
        r = compare_style(_pair(a, f))
        if r is None:
            return None                               # not reviewed, never a silent pass
        if not r["same_style"]:
            flagged.append(i)
            detail = detail or r["style_detail"]
        if r["same_person"] is not None:
            people[i] = r["same_person"]
            if not r["same_person"]:
                pdetail = pdetail or r["person_detail"]
    whole = len(flagged) * 2 > len(frames)
    return {"flagged": flagged, "whole_reel": whole,
            "detail": detail or "every clip matches the anchor",
            "people": people, "person_detail": pdetail}


def style_scan_cached(frames):
    """style_scan, answered ONCE per set of frames.

    Two checks ask this same question (the look, and the recurring character), and a vision model
    is not deterministic — calling it twice would hand the two checks different answers to the
    same question, and any cross-check built on that would be comparing the model with itself on
    a coin flip. Cache on the frame bytes: the answer is identical because it IS the same answer."""
    import hashlib
    key = hashlib.sha256(b"|".join(frames) + str(len(frames)).encode()).hexdigest()
    if key not in _SCAN_CACHE:
        _SCAN_CACHE.clear()                  # one reel at a time; frames are ~200KB each
        _SCAN_CACHE[key] = style_scan(frames)
    return _SCAN_CACHE[key]


_SCAN_CACHE = {}


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


def read_credits(png: bytes):
    """Screenshot -> this account's credit balance shown on the page, or None.
    One vision call, no per-site selector to drift — so every adapter gets a live read.
    None means 'not clear' (no balance visible, nonsense answer, gateway down): the caller
    then keeps the last known number instead of writing a guess over it."""
    prompt = (
        "This screenshot is a video-generation website. What balance of credits / coins / "
        "quota does THIS account have remaining? Reply ONLY JSON:\n"
        '{"credits": <non-negative integer>} or {"credits": null}\n'
        "Use null when no balance is visible or you are unsure. Count only the remaining "
        "balance for this account — never prices, plan limits, or 'X of Y used' counters."
    )
    try:
        d = _json_from(_ask(png, prompt, 300, patience=1))
    except Exception:
        return None
    v = d.get("credits")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    if v < 0 or v > 10_000_000:                 # nonsense is not a balance
        return None
    return v


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

        # A pinned VISION_MODEL must be the model actually SENT. Leaving it 'auto' lets the router
        # answer with a different vision model per call, so one unchanged frame scores 3d and
        # photoreal in the same run — measured 3/3, and the style gate then rejected a finished
        # reel on a coin flip. The pin is the fix; this keeps a later edit from quietly dropping it.
        assert _models_for("http://localhost:3001/v1")[0] == _MODEL, \
            "a pinned VISION_MODEL must be sent first, never left to the router's 'auto'"
        # ...and 'auto' must not be a SILENT fallback behind it. Measured 2026-10-10: the pin
        # rate-limited, 'auto' answered with 6,900 chars of prose and no JSON, and the gate
        # reported "vision gateway unavailable" while the gateway was answering fine.
        assert "auto" not in _models_for("http://localhost:3001/v1") or _MODEL == "auto", \
            "a pinned model must not fall back to 'auto' — it hides the real failure"
        print("OK: the vision model is PINNED (an unpinned 'auto' answers one frame two ways)")

        # defect filtering: an INSTRUCTION ECHO must never reach the gate, a real finding must
        echo = _clean_defects([{"type": "other", "detail": "short specific phrase"}])
        assert echo == [], f"instruction echo must be filtered, got {echo}"
        real = _clean_defects([{"type": "garbled-text", "detail": "the mug shows meaningless squiggles"}])
        assert real and real[0]["type"] == "garbled-text", f"a real defect must survive: {real}"
        assert _clean_defects([{"type": "other", "detail": "clean"}]) == [], "enum echo filtered"
        print("OK: defect filter drops instruction/enum echoes but keeps a described defect")

        # live balance read: parses a number, rejects nonsense, fails open to None
        def _bal(txt):
            return lambda png, p, *a, **k: txt
        _ask = _bal('{"credits": 42}')
        assert read_credits(b"x") == 42, "a visible balance must come back as an int"
        _ask = _bal('no idea {"credits": null}')
        assert read_credits(b"x") is None, "unclear balance -> None (keep the last known number)"
        _ask = _bal('{"credits": -3}')
        assert read_credits(b"x") is None, "nonsense rejected"
        _ask = _bal('{"credits": 999999999}')
        assert read_credits(b"x") is None, "absurd value rejected"
        def _down(png, p, *a, **k):
            raise RuntimeError("gateway down")
        _ask = _down
        assert read_credits(b"x") is None, "gateway down -> None, never an exception"
        print("OK: read_credits parses a live balance, rejects nonsense, fails open to None")

        # medium_verdict: the evidence rule that stops the style gate both missing a real
        # photoreal clip and re-rendering a good one over one ambiguous frame.
        assert medium_verdict(["photoreal", "3d"])[0] == "drift", \
            "a photoreal frame among 3-D ones is the drift this check exists for"
        assert medium_verdict(["3d", "mixed", "3d"])[0] == "uniform", \
            "one 'mixed' frame among 3-D is the AR-filter case, not drift"
        assert medium_verdict(["mixed", "mixed"])[0] == "drift", \
            "repeated mixing is corroboration"
        assert medium_verdict([None, None])[0] == "unknown", \
            "an unusable classifier is 'unknown' — never silently clean"
        # A PHOTOREALISTIC reel is consistent with ITSELF. Treating uniform-photoreal as drift
        # would have declared apne / haath_thaame / roz_ki_list inconsistent and HELD them.
        assert medium_verdict(["photoreal"] * 5)[0] == "uniform", \
            "a photorealistic reel is not drifting from itself"
        assert medium_verdict(["3d"] * 5)[0] == "uniform"
        # intent is a SEPARATE question, answered only when the caller knows it
        assert medium_verdict(["photoreal"] * 5, expected="3d")[0] == "wrong-medium", \
            "a photoreal clip where the identity asks for 3-D is wrong, not merely different"
        assert medium_verdict(["3d"] * 5, expected="3d")[0] == "uniform"
        assert medium_verdict(["3d"] * 5, expected="photoreal")[0] == "wrong-medium"
        print("OK: medium_verdict judges AGREEMENT (drift / wrong-medium / uniform / unknown); "
              "a photorealistic reel is not drifting from itself")

        # style_scan: the anchor comparison that can NAME a clip (medium_of cannot — see above),
        # plus the guard that stops it re-shooting a majority.
        _orig_cs = compare_style
        # real 4x6 PNGs, so _pair itself is exercised rather than stubbed away
        import io as _io
        from PIL import Image as _Im
        _png = []
        for _c in ((200, 40, 40), (40, 200, 40), (40, 40, 200)):
            _b = _io.BytesIO()
            _Im.new("RGB", (4, 6), _c).save(_b, "PNG")
            _png.append(_b.getvalue())
        assert _Im.open(_io.BytesIO(_pair(_png[0], _png[1], height=6))).size == (18, 6), \
            "two 4x6 frames -> one 18x6 strip (4 + 10 seam + 4)"
        _f = lambda n: [_png[i % 3] for i in range(n)]
        try:
            globals()["compare_style"] = lambda p: {"same_style": True, "style_detail": "",
                                                    "same_person": True, "person_detail": ""}
            s = style_scan(_f(3))
            assert s["flagged"] == [] and s["people"] == {1: True, 2: True}, \
                f"ONE call per clip answers both questions: {s}"
            # exactly one clip differs from the anchor -> that clip, and NOT a whole-reel problem
            globals()["compare_style"] = lambda p: {"same_style": False, "style_detail": "photoreal",
                                                    "same_person": True, "person_detail": ""}
            s = style_scan(_f(5))
            assert s["flagged"] == [1, 2, 3, 4] and s["whole_reel"], \
                "everything differing from the anchor is a whole-reel problem, not a requeue"
            calls = []

            def _one_bad(p, _c=calls):
                _c.append(p)
                same = len(_c) != 2                       # the 2nd comparison (clip 2) differs
                return {"same_style": same, "style_detail": "" if same else "much more realist2",
                        "same_person": True, "person_detail": ""}
            globals()["compare_style"] = _one_bad
            s = style_scan(_f(4))
            assert s["flagged"] == [2] and not s["whole_reel"], f"the odd clip must be named: {s}"
            assert s["detail"] == "much more realist2", "the reason must travel with the blame"
            globals()["compare_style"] = lambda p: None
            assert style_scan(_f(2)) is None, "gateway down -> None, never a silent pass"
            assert style_scan(_f(1))["flagged"] == [], "one clip has nothing to compare against"
            # a drifting FACE is named as precisely as a drifting look, and an answerless
            # person question (None) is simply absent — never recorded as "same person"
            globals()["compare_style"] = lambda p: {"same_style": True, "style_detail": "",
                                                    "same_person": False,
                                                    "person_detail": "she is wearing a bindi here"}
            s = style_scan(_f(3))
            assert s["people"] == {1: False, 2: False} and s["person_detail"], s
            globals()["compare_style"] = lambda p: {"same_style": True, "style_detail": "",
                                                    "same_person": None, "person_detail": ""}
            assert style_scan(_f(3))["people"] == {}, "an unparsed answer is not 'same person'"
            # the CACHE is what keeps the two checks comparing the same answer, not two rolls
            _hits = []
            globals()["compare_style"] = lambda p, _h=_hits: (
                _h.append(p), {"same_style": True, "style_detail": "", "same_person": True,
                               "person_detail": ""})[1]
            _SCAN_CACHE.clear()
            style_scan_cached(_f(3))
            style_scan_cached(_f(3))
            assert len(_hits) == 2, f"one answer per frame set, not per call: {len(_hits)}"
            _SCAN_CACHE.clear()
        finally:
            globals()["compare_style"] = _orig_cs
        print("OK: style_scan names the odd clip against the anchor, answers the character "
              "question in the same call, and refuses to re-shoot a majority")

        # An ANIMATED film is not defective for being animated. This shipped as a publish-gate
        # that HELD a finished reel ("3D animated CGI style instead of photorealism"), because
        # the reviewer was framed as a photorealist. The note is dropped — but ONLY when the
        # frame is uniform, so genuine drift ("her face photoreal while his is 3D animated")
        # must still survive. Both directions are asserted.
        for _d in ("3D animated CGI style instead of photorealism",
                   "cartoon style rather than photoreal", "3-D animated film, not real footage"):
            assert _clean_defects([{"type": "style", "detail": _d}]) == [], \
                f"naming the medium is not a defect: {_d}"
        for _d in ("her face photoreal while his is 3D animated", "part cartoon, part photoreal",
                   "style shifts mid-frame", "looks half cartoon half real"):
            assert len(_clean_defects([{"type": "style", "detail": _d}])) == 1, \
                f"REAL drift must not be filtered away: {_d}"
        assert len(_clean_defects([{"type": "limbs", "detail": "3D animated man has six fingers"}])) == 1, \
            "this exemption is scoped to `style` findings only"
        print("OK: an animated frame is not a defect, but a frame whose looks DISAGREE still is")
    finally:
        _ask = _orig

    if "--selfcheck" in sys.argv:
        # offline half only — `python3 automation/vision.py` (no flag) also runs the live
        # gateway round-trip below, which spends a couple of vision tokens.
        print("OK: vision --selfcheck is offline (live gateway round-trip: run without the flag)")
        raise SystemExit(0)

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
