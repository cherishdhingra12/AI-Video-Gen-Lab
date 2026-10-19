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
# extra routes to try if the primary 404s/errors (Google aliases). Checked against the live
# /models list 2026-10-13: 'gemini-3-flash-latest' is NOT one of them (404), and a 404 used to
# cool the whole endpoint — see the 404 branch in _ask.
_FALLBACK_MODELS = ["gemini-flash-latest", "auto/pro-vision"]

# Vision models the configured gateways really serve, best judgement first, asked ONLY when the
# pin cannot answer. Rate limits are per MODEL and per KEY, so a spent model is not a spent
# gateway: FreeLLMAPI's own catalog (2026-10-13) lists 45 enabled vision models over 14 platforms
# with separate pools — google's flash family rpd 20 x 3 keys, groq qwen3.8-27b 250, requesty 200,
# cohere aya-vision 33, cloudflare 10k neurons/day, nvidia 40 rpm x 3 keys. This pipeline asked
# ONE of them ('gemini-3.8-flash'); when that pool ran dry the craft gate lost its eyes for the
# day while 44 models sat idle. Every name here is a real seeing model — 'auto' is deliberately
# NOT among them (a router's pick returned 6,900 characters of prose and no JSON, 2026-10-10).
_FALLBACK_VISION = [
    "gemini-2.5-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-2.5-flash-lite", "qwen/qwen3.8-27b", "Qwen2.5-VL-72B-Instruct",
    "meta/llama-3.2-90b-vision-instruct", "command-a-vision-07-2025", "glm-4.6v-flash",
]
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
        gem = [m for m in [_MODEL] + _FALLBACK_MODELS + _FALLBACK_VISION if m.startswith("gemini")]
        seen, out = set(), []
        for m in gem:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out or ["gemini-flash-latest"]
    if "20128" in u or "omniroute" in u:
        return ["auto/vision", "auto/pro-vision", "auto/best-vision"]
    # A PINNED model beats the router's own pick. 'auto' on a router may answer with a DIFFERENT
    # vision model on every call, so the same unchanged frame gets different verdicts — measured:
    # six identical calls on one frame returned 3d x3 / photoreal x3, and the style gate then
    # rejected a finished reel on a coin flip. Pin VISION_MODEL in .env; 'auto' is the last
    # resort, never the default. (FreeLLMAPI does serve gemini-3.8-flash, so the fast local
    # route is kept AND the model is fixed.)
    if _MODEL and _MODEL != "auto":
        # The pin leads, and 'auto' is never in the list: a router's pick may be a NON-SEEING
        # model that answers anyway — measured 2026-10-10, a rate-limited pin fell through to
        # 'auto', which returned 6,900 characters of reasoning prose and no JSON, so
        # compare_character reported "vision gateway unavailable" while the gateway was
        # answering fine. What follows the pin is not 'auto' but a list of NAMED seeing models,
        # and the route only gives up when every one of them is throttled (see _ask): the pin
        # still decides every verdict it can answer, so its determinism is untouched.
        return [_MODEL] + [m for m in _FALLBACK_VISION if m != _MODEL]
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


def _post(url: str, key: str, model: str, png: bytes, prompt: str, max_tokens: int,
          timeout: int = 30):
    """One request to one (gateway, model). Returns (text, the model the SERVER reports) and raises
    whatever urllib raises, so the caller classifies the failure. Shared by _ask and _sees so a
    probe travels the same road a real question does."""
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
        ]}],
    }).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    return txt, (d.get("model") or model)


_SEES = {}          # (url, model) -> bool, decided once per process


def _probe_image() -> bytes:
    """A solid green frame, built rather than committed. The answer counts as 'I can see' only if
    the model names a colour it was never told."""
    import io as _io
    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (160, 120), (0, 200, 0)).save(buf, "PNG")
    return buf.getvalue()


def _sees(url: str, key: str, model: str) -> bool:
    """Has THIS (gateway, model) ever named the colour of an image it was sent?

    The dangerous failure is not a gateway that is down — that scores 'not reviewed'. It is a
    gateway that answers while BLIND: a router whose vision route resolves to a text model
    (OmniRoute's auto/vision answered as claude-fable-5) returns a perfectly well-formed
    {"same_person": true, "defects": []} for a frame it never saw, and every check downstream reads
    that as a clean reel. An outage is loud; a blind 'clean' is invisible, which is why the route
    has to prove it can see before its silence is believed.

    Cached per (gateway, model) per process. A throttle on the PROBE is not blindness — the route
    just answered a real question with an image attached — so it is not cached and not held
    against it; a definite answer without the colour, or a hard error, is."""
    k = (url, model)
    if k not in _SEES:
        prompt = "What single colour fills this image? Answer with just the colour word."
        try:
            txt, _ = _post(url, key, model, _probe_image(), prompt, 16)
            _SEES[k] = "green" in txt.lower()
        except urllib.error.HTTPError as e:
            if e.code in _TRANSIENT:
                return True                     # busy, not blind: let this answer stand
            if e.code in (400, 404):
                # The probe hit a name this gateway does not serve — a fact about the MODEL, not
                # about anyone's eyes. Branding it "cannot see" would discard real verdicts over a
                # typo, which is the same conflation as cooling a route for a stale alias.
                _SEES[k] = True
                return True
            _SEES[k] = False
        except Exception:
            _SEES[k] = False
        if not _SEES.get(k, True):
            print(f"  [vision] {url} {model} cannot see (no colour named for the probe image) — "
                  f"its 'clean' verdicts are discarded")
    return _SEES[k]


def _ask(png: bytes, prompt: str, max_tokens: int = 600, patience: int = 2) -> str:
    """Ask a vision model, failing over across gateways and routes. On a transient throttle /
    high-demand code (429/503), retry the whole sweep a few times with backoff — so a momentary
    spike doesn't blind the self-heal. Fail-open: callers treat a raise as 'unknown'.

    A route the LAST call found walled is skipped, not re-discovered: the 90s timeout and the
    sweep backoff make a dead gateway the most expensive thing in the run, and the craft gate
    makes dozens of vision calls per reel (routerhealth). If everything is cooling, everything is
    tried anyway — a memory of failure must never be the reason a check cannot run."""
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
            tried = throttled = 0
            for model in _models_for(url):          # … each with a model dialect it accepts
                tried += 1
                try:
                    txt, served = _post(url, key, model, png, prompt, max_tokens)
                    if txt.strip():
                        if not _sees(url, key, model):
                            last = RuntimeError(f"{url} {model} answered without seeing the image")
                            continue    # a blind route's answer is an outage wearing a verdict
                        # the model the SERVER reports, not the one we asked for: a gateway is free
                        # to route 'auto', and the difference is exactly what a failover hides
                        ANSWERED.append({"url": url, "asked": model, "model": served})
                        routerhealth.note_success(url)
                        return txt
                    routerhealth.note_failure(url, "empty content")
                except urllib.error.HTTPError as e:
                    if e.code in (400, 404):
                        # A NAME, not the gateway. 404 is a model this endpoint does not serve;
                        # a 400 here is its "unsupported/ambiguous model" — measured live on
                        # 2026-10-13, OmniRoute answers one for a bare name it cannot map
                        # ("Use a provider/model prefix"). This endpoint answers 200 on its other
                        # models, and cooling the route for a name took Google direct out of the
                        # vision rotation entirely while a stale alias sat third in
                        # _FALLBACK_MODELS: a dead name in a list is not a dead route.
                        continue
                    last = e
                    if e.code in _TRANSIENT:
                        # A 429 on an aggregator is a PER-MODEL, PER-KEY pool (gemini flash rpd 20,
                        # groq 250, requesty 200): the next model is a different, unspent pool. So
                        # try it, and wall the route only when EVERY model on it was throttled —
                        # otherwise one exhausted model takes the whole gateway down with it, which
                        # is exactly how the craft gate lost its eyes on 2026-10-13.
                        transient = True
                        throttled += 1
                        continue
                    routerhealth.note_failure(url, f"HTTP {e.code}")
                except Exception as e:
                    last = e
                    routerhealth.note_failure(url, e)
                    break         # a connection error is about the ENDPOINT: every model hits it
            if throttled and throttled == tried:
                routerhealth.note_failure(url, getattr(last, "code", "429"))
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


def compare_character(png: bytes, allow_age_change=False):
    """Review a CONTACT SHEET (one panel per scene) of the film's recurring character and say
    whether it is the same person throughout. Returns {'consistent': bool, 'detail': str}, or
    None if the gateway was unavailable — so the gate can say 'not reviewed' instead of passing.

    The sheet is ONE image on purpose: the gateway takes a single image per call, and a montage
    is exactly the question 'same person in every panel?', which no single frame can answer.

    `allow_age_change` is for a sheet the caller KNOWS mixes two points in the story's timeline (a
    declared flashback it could not keep out). There the older/younger pair IS the story. It
    defaults to False and stays off for a single-period sheet, where a change of age is still
    evidence of a different person."""
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
    if allow_age_change:
        prompt += (
            "\nIMPORTANT: this sheet may mix two points in the timeline, because the film has a "
            "flashback. The same character may therefore appear YOUNGER in some panels, and that "
            "is the story working — NOT an inconsistency. Judge identity by the face itself "
            "(shape, features, hair, skin tone) and reply same=true when age is the only thing "
            "that differs.")
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
    "These are two frames from the SAME short film, side by side (left, right). Answer three "
    "separate questions. (0) presence: is a PERSON clearly visible in the left frame, and in the "
    "right frame? A room, a piece of furniture, a landscape, or a framed photograph hanging on a "
    "wall is NOT a person in the frame. (1) art style: are the two frames rendered in the SAME "
    "technique and the same level of realism/detail, or is one clearly more photoreal, or more "
    "cartoonish, than the other? Ask this only when a person is visible in BOTH frames — a "
    "still-life and a face have no common style to compare, and answering anyway turns the room "
    "into a false 'style drift'. Anything INSIDE the scene — a family photograph on a wall, a "
    "poster, a painting, a picture on a screen — is part of the scene, so what it depicts is "
    "NEVER a difference in technique: never report 'one includes a photograph' as a difference. "
    "(2) person: the film has ONE recurring main character, and other people (children, "
    "passers-by) may appear too. Is the MAIN character the same individual in both frames — same "
    "face shape, eyes, age, features? Clothing, lighting and pose may legitimately differ, and a "
    "film may be 3-D animation: never report 'this is animation' as a difference. When a frame "
    "clearly shows a DIFFERENT member of the cast (a child where the other frame shows the "
    "adult) rather than the main character drawn differently, that is casting, not drift — leave "
    "same_person null rather than false. "
    'Reply ONLY JSON: {"people_visible": [true|false, true|false], "same_style": true|false, '
    '"style_detail": "short phrase", "same_person": true|false|null, '
    '"person_detail": "short phrase"}. If either frame shows no person, same_person MUST be null '
    "— an absent person is not a different person."
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
    # WHICH FRAMES HAVE A PERSON IN THEM. The style and person questions are both about how a
    # CHARACTER is rendered, so a pair where one frame is furniture cannot answer either — asked
    # anyway, the model says "one is photorealistic, the other is cartoonish" and the room is
    # convicted as style drift (measured 2026-10-14: the wall of framed photographs in dadaji clip
    # 1 was flagged as a reel whose clips do not match). None when unanswered -> the caller keeps
    # the old, stricter behaviour rather than quietly widening the gate.
    vis = d.get("people_visible")
    out["people_visible"] = ([bool(x) for x in vis] if isinstance(vis, list) and len(vis) == 2
                             and all(isinstance(x, bool) for x in vis) else None)
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

        {'flagged': [indices], 'unreviewed': [indices], 'whole_reel': bool, 'detail': str,
         'people': {index: bool}, 'person_detail': str}

    or None when the gateway could not answer. The SAME comparison answers two questions —
    'same look?' and 'same person?' — because they are what one clip-vs-clip call can see, and one
    call per clip is what the budget allows. (`whole_reel`/`detail` are the look; `people` is the
    character, keyed by clip index, so a drifting face can be named as precisely as a drifting
    style.)

    Both questions are about how a CHARACTER is drawn, so a pair where one frame holds no person
    answers neither: `unreviewed` names those clips. They are not a pass — a person should still
    look — so the caller must say so out loud rather than let silence read as clean.

    `whole_reel` is the honest guard: when MORE than half the clips differ from the anchor the
    anchor itself is the odd one out, and re-shooting the majority would be re-shooting the reel.
    That is a decision for a human, not a requeue, so those come back flagged as whole-reel and
    the caller must not auto-regenerate. It also protects the grandfathered photoreal reels: a
    photoreal reel whose scenes read 'different' to each other is never silently re-shot."""
    if len(frames) < 2:
        return {"flagged": [], "unreviewed": [], "whole_reel": False, "detail": "", "people": {},
                "person_detail": ""}
    a = frames[anchor]
    flagged, unreviewed, detail, people, pdetail = [], [], "", {}, ""
    for i, f in enumerate(frames):
        if i == anchor:
            continue
        r = compare_style(_pair(a, f))
        if r is None:
            return None                               # not reviewed, never a silent pass
        seen = r.get("people_visible")
        if seen is not None and not all(seen):
            # ONE FRAME HAS NO PERSON IN IT, so this pair cannot testify about the character's
            # look or identity — that is the question it was asked. It is not a pass (a person
            # should still look), so it comes back in its own channel. The per-clip medium check
            # is what catches a still-life that rendered photoreal in an animated reel; this
            # comparison only ever judged character shots against each other.
            unreviewed.append(i)
            continue
        if not r["same_style"]:
            flagged.append(i)
            detail = detail or r["style_detail"]
        if r["same_person"] is not None:
            people[i] = r["same_person"]
            if not r["same_person"]:
                pdetail = pdetail or r["person_detail"]
    whole = len(flagged) * 2 > len(frames)
    return {"flagged": flagged, "unreviewed": unreviewed, "whole_reel": whole,
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
            # A FRAME WITH NO PERSON IN IT CANNOT TESTIFY. A still-life (a table, a wall of framed
            # photographs) against a character close-up is a comparison with no common subject,
            # and the model still answers "one is photorealistic, the other is cartoonish" — which
            # is how dadaji clip 1 was convicted on 2026-10-14 for a frame that was a photo wall.
            # It is not a pass: the clip comes back in the unreviewed channel, and a person looks.
            globals()["compare_style"] = lambda p: {"same_style": False, "style_detail": "one photoreal",
                                                    "same_person": False, "person_detail": "no face",
                                                    "people_visible": [True, False]}
            s = style_scan(_f(4))
            assert s["flagged"] == [] and s["unreviewed"] == [1, 2, 3] and s["people"] == {}, \
                f"a pair with no person in one frame convicts nobody: {s}"
            assert not s["whole_reel"], "an unreviewed clip is not a whole-reel problem either"
            globals()["compare_style"] = lambda p: {"same_style": False, "style_detail": "one photoreal",
                                                    "same_person": False, "person_detail": "no face",
                                                    "people_visible": [False, False]}
            assert style_scan(_f(4))["unreviewed"] == [1, 2, 3], "neither frame has a person"
            # two CHARACTER shots that differ are still convicted, and an ANSWERLESS presence
            # question keeps the old stricter behaviour rather than quietly widening the gate
            globals()["compare_style"] = lambda p: {"same_style": False, "style_detail": "much more realist2",
                                                    "same_person": True, "person_detail": "",
                                                    "people_visible": [True, True]}
            s = style_scan(_f(4))
            assert s["flagged"] == [1, 2, 3] and s["unreviewed"] == [], \
                f"both frames hold a person -> the look question is answerable: {s}"
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
        # offline half — `python3 automation/vision.py` (no flag) also runs the live gateway
        # round-trip below, which spends a couple of vision tokens.
        # Fault-injected, one assertion per way this cost the gate its eyes on 2026-10-13.
        import io
        import urllib.error
        import routerhealth as _rh
        _o_open, _o_ends, _o_models = urllib.request.urlopen, _endpoints, _models_for
        hits = []

        def _reply(model, prompt, blind=False):
            """A seeing stub names the probe's colour; a blind one answers everything with prose,
            which is exactly how a router whose 'vision' route is a text model behaves."""
            if "colour" in prompt:
                return "I cannot see any image here." if blind else "green"
            return "{}"

        def _body(model, prompt, blind=False):
            return io.BytesIO(json.dumps(
                {"model": model, "choices": [{"message": {"content": _reply(model, prompt, blind)}}]}
            ).encode())

        def _fake(req, timeout=90):
            m = json.loads(req.data)["model"]
            prompt = req.data.decode()
            hits.append(m)
            if m == "dead-name":
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
            return _body(m, prompt)

        def _throttle_first(req, timeout=90):
            m = json.loads(req.data)["model"]
            prompt = req.data.decode()
            hits.append(m)
            if m == "spent-model":
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)
            return _body(m, prompt)

        def _throttled(req, timeout=90):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

        def _dead(req, timeout=90):
            hits.append(json.loads(req.data)["model"])
            raise OSError("connection refused")

        def _mixed(req, timeout=90):
            m = json.loads(req.data)["model"]
            hits.append(m)
            raise urllib.error.HTTPError(req.full_url, 429 if m == "spent-model" else 404,
                                         "x", {}, None)

        try:
            # (1) THE CAPACITY FIX. 45 vision models sit behind these gateways with separate
            # per-model, per-key pools; asking one is what ran a whole day's gate dry.
            for _u in ("http://localhost:3001/v1", "https://api.example.com/v1"):
                _ms = _models_for(_u)
                assert len(_ms) > 1, f"a vision gateway must offer its other pools too: {_ms}"
                assert _ms[0] == _MODEL, f"the pin still leads: {_ms}"
                assert "auto" not in _ms, f"'auto' may pick a model that cannot see: {_ms}"
            _ms = _models_for("https://generativelanguage.googleapis.com/v1beta/openai")
            assert len(_ms) > 1 and all(m.startswith("gemini") for m in _ms), _ms
            # (2) a 404 is a bad NAME; a route whose only name is stale must not be walled for it
            urllib.request.urlopen = _fake
            globals()["_endpoints"] = lambda: [("http://t/v1", "k")]
            globals()["_models_for"] = lambda u: ["dead-name"]
            _rh.clear()
            try:
                _ask(b"x", "hi", patience=0)
            except Exception:
                pass
            assert not _rh.cooling("http://t/v1")[0], "a stale model name is not a dead gateway"
            # …and a list that has a live name after the stale one still answers
            globals()["_models_for"] = lambda u: ["dead-name", "good-name"]
            hits.clear()
            _rh.clear()
            assert _ask(b"x", "hi") == "{}", "a 404 on one model must fall through to the next"
            assert hits[:2] == ["dead-name", "good-name"], hits
            # (3) a 429 on an aggregator is a PER-MODEL pool: the next model is unspent
            urllib.request.urlopen = _throttle_first
            globals()["_models_for"] = lambda u: ["spent-model", "fresh-model"]
            hits.clear()
            _rh.clear()
            assert _ask(b"x", "hi") == "{}", "a spent model must fall through to an unspent one"
            assert hits[:2] == ["spent-model", "fresh-model"], hits
            assert not _rh.cooling("http://t/v1")[0], \
                "one exhausted model must not take the whole gateway out of the rotation"
            # a route is walled for being OUT OF CAPACITY, not merely for having a spent model
            # beside a stale name: if not every model was throttled, the wall would hide whatever
            # the next call could still reach
            globals()["_models_for"] = lambda u: ["spent-model", "dead-name"]
            urllib.request.urlopen = _mixed
            hits.clear()
            _rh.clear()
            try:
                _ask(b"x", "hi", patience=0)
            except Exception:
                pass
            assert not _rh.cooling("http://t/v1")[0], \
                "only an every-model throttle is a route out of capacity"
            # (4) …but a gateway whose every model is throttled IS out of capacity, and is
            # remembered, so the next call does not re-walk all of them
            urllib.request.urlopen = _throttled
            globals()["_models_for"] = lambda u: ["m1", "m2"]
            _rh.clear()
            try:
                _ask(b"x", "hi", patience=0)
            except Exception:
                pass                          # out of routes: the caller handles it
            assert _rh.cooling("http://t/v1")[0], "429 on EVERY model must wall the route"
            # (5) a connection error is the ENDPOINT's problem, so it must not be re-discovered
            # once per model name — that is 90s of timeout each
            hits.clear()
            urllib.request.urlopen = _dead
            globals()["_models_for"] = lambda u: ["m1", "m2", "m3"]
            _rh.clear()
            try:
                _ask(b"x", "hi", patience=0)
            except Exception:
                pass
            assert hits == ["m1"], f"a dead gateway must be walled once, not per model: {hits}"
            assert _rh.cooling("http://t/v1")[0], "a connection error must wall the route"
            # (6) A ROUTE THAT ANSWERS WHILE BLIND IS AN OUTAGE, NOT A CLEAN REEL. A router whose
            # 'vision' route is really a text model returns well-formed JSON for a frame it never
            # saw — and 'no defects, same person' downstream is a reel nobody checked.
            def _blind(req, timeout=90):
                m = json.loads(req.data)["model"]
                hits.append(m)
                return _body(m, req.data.decode(), blind=True)

            urllib.request.urlopen = _blind
            globals()["_models_for"] = lambda u: ["blind-model"]
            hits.clear(), _rh.clear(), _SEES.clear()
            try:
                _ask(b"x", "hi", patience=0)
                raise AssertionError("a blind route's answer must never be returned as a verdict")
            except RuntimeError as e:
                assert "without seeing" in str(e), e
            assert hits == ["blind-model"] * 2, f"one real call, then one probe: {hits}"
            hits.clear()                     # …and the verdict is remembered, not re-bought
            try:
                _ask(b"x", "hi", patience=0)
            except RuntimeError:
                pass
            assert hits == ["blind-model"], f"the probe is asked once per route: {hits}"
            # (7) A 400 IS ALSO A NAME. Measured live on 2026-10-13: OmniRoute answers 400
            # ("Ambiguous model 'gemini-2.5-flash'. Use a provider/model prefix") for a bare name
            # it cannot map, and treated as a route failure it cooled the gateway for the whole
            # vision role — the stale-404 conflation, one status code over.
            def _nameless(req, timeout=90):
                m = json.loads(req.data)["model"]
                hits.append(m)
                if m == "bare-name":
                    raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
                return _body(m, req.data.decode())

            urllib.request.urlopen = _nameless
            globals()["_models_for"] = lambda u: ["bare-name"]
            hits.clear(), _rh.clear(), _SEES.clear()
            try:
                _ask(b"x", "hi", patience=0)
            except Exception:
                pass
            assert not _rh.cooling("http://t/v1")[0], "an unmappable name is not a dead route"
            globals()["_models_for"] = lambda u: ["bare-name", "provider/model"]
            hits.clear()
            assert _ask(b"x", "hi", patience=0) == "{}", "a 400 on one name must fall through"
            assert hits[:2] == ["bare-name", "provider/model"], hits

            # …and a probe that 404s is a fact about the NAME: it must not brand the route blind
            # and throw away an answer the route really gave.
            def _probe404(req, timeout=90):
                m = json.loads(req.data)["model"]
                hits.append(m)
                if "colour" in req.data.decode():
                    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
                return _body(m, req.data.decode())

            urllib.request.urlopen = _probe404
            globals()["_models_for"] = lambda u: ["probe-404"]
            hits.clear(), _SEES.clear()
            assert _ask(b"x", "hi", patience=0) == "{}", "a 404 on the probe is not blindness"
            assert _SEES == {("http://t/v1", "probe-404"): True}, _SEES
        finally:
            urllib.request.urlopen = _o_open
            globals()["_endpoints"], globals()["_models_for"] = _o_ends, _o_models
            _SEES.clear()
            _rh.clear()
        print("OK: vision --selfcheck is offline (a 404 skips the model, a spent model skips to the "
              "next pool, a dead gateway is walled once, a blind route is discarded; live gateway "
              "round-trip: run without the flag)")
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
