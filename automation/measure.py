"""
measure.py — assign each agent its model by MEASURING it, not by taste.

The two routers between them expose ~1,200 models, and which one suits which agent is not a
matter of taste: every role in this pipeline already has a CONTRACT saying what a good answer
looks like, so the answer can be SCORED instead of admired. One real call per candidate per role:

    script        the sections the writer was asked for, in Hindi, long enough to shoot
    shotlist      a plan surviving build_shotlist's own rules — 3-D style, 3-6 clips, no clip
                  prompt asking for text, no camera travelling INTO anything, real dialogue
    presentation  agent 3's output passing agents._presentation_selfcheck (identity + cards)
    judge         the caption judge FINDS the planted defect and INVENTS none on clean cards
    vision        style_scan on labelled frames: clip 2 of dadi-ki-dhansuchi is the odd one (a
                  different woman, nearly photoreal), so it MUST come back flagged

Results land in automation/model_picks.json, which agents.chat(role=...) reads and leads with —
the rest of the order stays behind the winner, so a subordinate takes over without an interruption
whenever the winner is walled or slow.

    python3 automation/measure.py                 # every role
    python3 automation/measure.py --role judge     # one role
    python3 automation/measure.py --dry-run       # print the plan, fire nothing

A candidate that could not be RUN is recorded as unmeasured, never as a failure: an outage is not
a defect, and a route that was down at measuring time must not lose to the one that happened to
answer. The picks file is runtime state (like .brain_state.json) — a measurement of a free tier
is machine-local and goes stale, so it is gitignored and re-run by hand.

ponytail: the script role scores SHAPE (the sections exist, in Hindi, long enough) — nothing here
reads a screenplay. It is the weakest of the five and the report says so.
"""

import argparse
import copy
import json
import os
import re
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_DIR)
for _p in (_ROOT, _DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents      # noqa: E402
import qc          # noqa: E402
import routerhealth  # noqa: E402
import vision      # noqa: E402

PICKS = os.path.join(_DIR, "model_picks.json")

# The candidates worth the calls: the routers where a CHOICE exists. OmniRoute's `auto/*` routes
# pick their own model (that is the 1024-route router); FreeLLMAPI serves named models directly.
TEXT_CANDIDATES = [
    ("freellmapi", "gemini-3.8-flash"),
    ("freellmapi", "gpt-oss-120b"),
    ("freellmapi", "nemotron-3-ultra"),
    ("omniroute", "auto/best-chat"),
    ("omniroute", "auto/reasoning"),
]

_IDEA = "एक बूढ़े पिता की आवाज़, जो उनके बेटे के पुराने फ़ोन में बंद है"

_SCRIPT = (
    "LOGLINE: A son finds his late father's voice still locked in an old phone.\n"
    "VISUAL STYLE: soft stylized 3-D animation, warm amber lamplight, shallow focus.\n"
    "BEATS:\n"
    "1. A son scrolls an old phone at night; the cold screen lights his face. He says: "
    "क्या तुम्हें यह आवाज़ अब भी याद है?\n"
    "2. He finds a recording of his father humming, and stops moving. No line — the beat is "
    "silent, only the hum.\n"
    "3. He plays it for the old man, who smiles without saying anything: सुनो, यह उनकी आवाज़ है।\n"
    "4. The phone lies face up on the table between them, still playing, as the lamp warms the "
    "room and neither of them speaks."
)

_SHOT = {"title": "पुराना फ़ोन", "slug": "purana-phone", "aspect": "9:16",
         "world": ("A small living room at night. THE SON — a man in his forties in a grey "
                   "sweater. BAUJI — a frail old man in a white kurta, no glasses, no jewellery."),
         "style": ("Soft stylized 3-D animation in a high-end animated-feature look: rounded "
                   "shapes, soft global illumination, cinematic lighting."),
         "music_mood": "warm, nostalgic", "music_want": "soft instrumental"}

# The clean cards (aligned with the script, one beat each) and the PLANTED ones. The planted set
# carries two defects the judge was explicitly built to catch: a card that contradicts the script
# (a treasure, a moral about money) and one word spelled two ways in one reel (ख़ज़ाना / खज़ाना).
_CLEAN_CAPS = {"font": "rounded",
               "hook": {"lines": ["पुराने फ़ोन में एक आवाज़ बंद है"]},
               "moral": {"lines": ["यादें हमेशा साथ रहती हैं"]},
               "cta": {"lines": ["standardsunrisefoundation.org पर आएँ"]}}
_PLANTED_CAPS = {"font": "rounded",
                 "hook": {"lines": ["पुराने फ़ोन में ख़ज़ाना छिपा है"]},
                 "moral": {"lines": ["पैसा ही सब कुछ है", "खज़ाना सबसे बड़ा सच है"]},
                 "cta": {"lines": ["standardsunrisefoundation.org पर आएँ"]}}

# dadi-ki-dhansuchi's source clips are the labelled truth for the vision role: three different
# women play the grandmother, clip 002 is nearly photoreal, so 2 MUST be flagged against the
# anchor. Only the first three clips are sampled — the anchor plus two — to keep this cheap.
_VISION_REEL = "dadi-ki-dhansuchi"
_VISION_TRUTH = 2


def _lines(caps):
    out = []
    for k in ("hook", "moral", "cta"):
        out += [str(x) for x in ((caps or {}).get(k) or {}).get("lines") or []]
    return out


# ---- the scorers: pure functions of an answer, so each one is testable with no gateway -------

def _frac(checks) -> float:
    checks = list(checks)
    return sum(1 for c in checks if c) / len(checks) if checks else 0.0


def _score_script(text: str) -> float:
    """SHAPE only — see the module docstring. The sections the prompt asked for, in Hindi, with
    enough written to shoot."""
    up = (text or "").upper()
    return _frac([("LOGLINE" in up), ("BEAT" in up), ("STYLE" in up),
                  bool(re.search(r"[ऀ-ॿ]", text or "")), len(text or "") >= 500])


def _score_shotlist(shot) -> float:
    """build_shotlist's own rules, as a score. Every clause here is a defect that has actually
    shipped: a photoreal style, a plan too thin to cut, a prompt the model wrote text into, a
    camera sent INTO a frame, a reel with no spoken line at all."""
    if not isinstance(shot, dict):
        return 0.0
    clips = shot.get("clips") or []
    prom = [str(c.get("prompt") or "") for c in clips if isinstance(c, dict)]
    says = [str(c.get("dialogue") or "") for c in clips if isinstance(c, dict)]
    return _frac([
        agents._is_animated(shot.get("style") or ""),
        3 <= len(clips) <= 6,
        bool(prom) and all(len(p) >= 20 for p in prom),
        not any(agents._asks_text(p) for p in prom),
        not any(re.search(r"camer\w+[^.]{0,40}\b(?:into|inside)\b", p, re.I) for p in prom),
        len(str(shot.get("world") or "")) >= 80,
        str(shot.get("aspect") or "") == "9:16",
        any(re.search(r"[ऀ-ॿ]", s) for s in says),
    ])


def _score_judge(bad: dict, good: dict) -> float:
    """Three questions, equally weighted: was the planted defect NAMED, was it FIXED in the cards
    that came back, and were the clean cards left alone? A judge that flags everything scores 1/3
    — the same as one that flags nothing — which is the point: both are useless."""
    named = 1.0 if (bad.get("problems") or []) else 0.0
    fixed = 1.0 if _lines(bad.get("captions")) != _lines(_PLANTED_CAPS) else 0.0
    quiet = 1.0 if not (good.get("problems") or []) else 0.0
    return (named + fixed + quiet) / 3


def _score_vision(scan) -> float:
    """The labelled frame must be flagged. None (the gateway could not answer) is UNMEASURED, not
    a zero — handled by the caller, which passes None straight through."""
    if scan is None:
        return None
    return 1.0 if _VISION_TRUTH in (scan.get("flagged") or []) else 0.0


# ---- the trials: one real call per candidate ------------------------------------------------

def _pin(route, model):
    """Pin ONE text route for the call, with no failover: a candidate that fell back to another
    gateway is not the candidate being measured."""
    agents._PIN = (route, model)
    routerhealth.clear()


def _t_script(route, model) -> float:
    _pin(route, model)
    return _score_script(agents.write_script(_IDEA))


def _t_shotlist(route, model) -> float:
    _pin(route, model)
    return _score_shotlist(agents.build_shotlist(_SCRIPT))


def _t_presentation(route, model) -> float:
    """1.0 only if agent 3's output passed its own contract. presentation() already repairs once,
    so a raise here means it could not produce a shippable identity in two attempts."""
    _pin(route, model)
    agents._presentation_selfcheck(agents.presentation(_SCRIPT, _SHOT))
    return 1.0


def _t_judge(route, model) -> float:
    """TWO calls, because half of judging is not inventing: the planted set, then the clean set."""
    _pin(route, model)
    bad = agents.judge_captions(_SCRIPT, copy.deepcopy(_PLANTED_CAPS))
    good = agents.judge_captions(_SCRIPT, copy.deepcopy(_CLEAN_CAPS))
    return _score_judge(bad, good)


def _vision_candidates():
    """What production would actually try, in order: each endpoint with the first model its
    dialect accepts. Measuring a (gateway, model) pair that _models_for would never send is
    measuring a route this pipeline cannot use."""
    out = []
    for url, key in vision._endpoints():
        models = vision._models_for(url)
        if models:
            out.append((url, key, models[0]))
    return out


def _frames():
    """One frame per source clip, PNG bytes, reel order. Absent clips -> [] (unmeasured, not a
    failure: `clips/` is not committed, so a fresh clone simply cannot run this role)."""
    import types
    reel = types.SimpleNamespace(name=_VISION_REEL)
    try:
        return qc._clip_frames(reel, limit=3)
    except Exception as e:  # noqa: BLE001 — ffmpeg missing/absent clips is not a defect
        print(f"  vision frames unavailable: {str(e)[:100]}")
        return []


def _t_vision(url, key, model) -> float:
    """Pin the endpoint AND the model, because _ask would otherwise fail over to a different
    (gateway, model) pair mid-call and score the wrong thing."""
    frames = _frames()
    if len(frames) < 2:
        return None
    o_url, o_model = vision._endpoints, vision._MODEL
    try:
        vision._endpoints = lambda: [(url, key)]
        vision._MODEL = model
        vision._SCAN_CACHE.clear()
        routerhealth.clear()
        return _score_vision(vision.style_scan(frames))
    finally:
        vision._endpoints, vision._MODEL = o_url, o_model
        vision._SCAN_CACHE.clear()
        agents._PIN = None


TRIALS = {
    "script": (_t_script, TEXT_CANDIDATES),
    "shotlist": (_t_shotlist, TEXT_CANDIDATES),
    "presentation": (_t_presentation, TEXT_CANDIDATES),
    "judge": (_t_judge, TEXT_CANDIDATES),
    "vision": (_t_vision, None),              # candidates come from the endpoints themselves
}


def best(rows: list):
    """The winner: highest score, ties broken by the faster route. Rows that could not be RUN are
    skipped entirely — a blip must not hand a role to whichever candidate happened to answer."""
    measured = [r for r in rows if r.get("score") is not None]
    if not measured:
        return None
    return max(measured, key=lambda r: (r["score"], -r["secs"]))


def _label(cand, role):
    if role == "vision":
        return f"{cand[0].split('//')[-1].split('/')[0]}:{cand[2]}"
    return f"{cand[0]}:{cand[1]}"


def measure(roles, dry=False):
    picks = {}
    if os.path.exists(PICKS):
        try:
            picks = json.load(open(PICKS))
        except (OSError, ValueError):
            picks = {}
    table = []
    for role in roles:
        trial, cands = TRIALS[role]
        cands = _vision_candidates() if cands is None else cands
        rows = []
        for cand in cands:
            if dry:
                print(f"  [dry] {role:13s} {_label(cand, role)}")
                continue
            t0 = time.time()
            try:
                score = trial(*cand)
            except Exception as e:  # noqa: BLE001 — unmeasured, never a failure
                score = None
                print(f"  {role:13s} {_label(cand, role):34s} NOT MEASURED: {str(e)[:110]}")
            secs = time.time() - t0
            rows.append({"route": cand[0], "model": cand[-1], "score": score, "secs": secs})
            if score is not None:
                print(f"  {role:13s} {_label(cand, role):34s} {score:.2f}  ({secs:.0f}s)")
        if dry:
            continue
        win = best(rows)
        table.append((role, rows, win))
        if win:
            picks[role] = {"route": win["route"], "model": win["model"],
                           "score": round(win["score"], 3), "secs": round(win["secs"], 1),
                           "by": "measure.py", "ts": int(time.time())}
        elif role in picks:
            print(f"  {role}: nothing measured this run — keeping the previous pick")
    if not dry:
        with open(PICKS, "w") as f:
            json.dump(picks, f, indent=2, ensure_ascii=False)
    return table, picks


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", action="append", choices=sorted(TRIALS),
                    help="measure only this role (repeatable); default: all")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, fire nothing")
    ap.add_argument("--selfcheck", action="store_true", help="offline checks of the scorers")
    a = ap.parse_args()

    if a.selfcheck:
        assert abs(_frac([True, False, True, True]) - 0.75) < 1e-9, "a fraction of checks"
        assert _frac([]) == 0.0, "no checks must not divide by zero"
        # script: the sections the writer was told to produce, in Hindi, long enough to shoot
        assert _score_script(_SCRIPT) == 1.0, "a good script must score full marks"
        assert _score_script("LOGLINE: x") < 0.5, "a stub script must not"
        # shotlist: a plan the pipeline would accept, and three ways it has actually failed
        good = {"style": agents.HOUSE_STYLE, "world": "A room at night. " * 8, "aspect": "9:16",
                "clips": [{"prompt": "He scrolls the phone slowly, then stops.",
                           "dialogue": "यह आवाज़ कौन है?"}] * 3}
        assert _score_shotlist(good) == 1.0, f"a clean plan must score 1.0, got {_score_shotlist(good)}"
        photo = dict(good, style="photoreal documentary footage")
        assert _score_shotlist(photo) < 1.0, "a photoreal plan must lose points"
        texty = copy.deepcopy(good)
        texty["clips"][0]["prompt"] = 'A glowing "एक" appears on the page.'
        assert _score_shotlist(texty) < 1.0, "a prompt that asks for text must lose points"
        intr = copy.deepcopy(good)
        intr["clips"][0]["prompt"] = "The camera glides into the wall frame and pushes in."
        assert _score_shotlist(intr) < 1.0, "a camera travelling INTO a frame must lose points"
        short = dict(good, clips=good["clips"][:1])
        assert _score_shotlist(short) < 1.0, "a one-clip plan is not a reel"
        assert _score_shotlist(None) == 0.0, "no plan at all scores zero"
        # judge: naming the defect, fixing it, and staying quiet on clean cards are three marks
        named_fixed = {"problems": ["moral contradicts the script"],
                       "captions": copy.deepcopy(_CLEAN_CAPS)}
        assert _score_judge(named_fixed, {"problems": [], "captions": {}}) == 1.0, \
            "finding the defect, fixing it and staying quiet on clean cards is a full score"
        assert _score_judge({"problems": [], "captions": copy.deepcopy(_PLANTED_CAPS)},
                            {"problems": [], "captions": {}}) == 1 / 3, \
            "a judge that flags nothing and fixes nothing scores one mark out of three"
        assert _score_judge({"problems": ["x"], "captions": copy.deepcopy(_CLEAN_CAPS)},
                            {"problems": ["nitpick"], "captions": {}}) == 2 / 3, \
            "inventing a problem on clean cards must cost the mark"
        assert abs(_score_judge(named_fixed, {"problems": [], "captions": {}}) - 1.0) < 1e-9
        # vision: the labelled clip, and the outage that must NOT read as a zero
        assert _score_vision({"flagged": [1, 2]}) == 1.0, "the labelled clip must be found"
        assert _score_vision({"flagged": [1]}) == 0.0, "missing the labelled clip is a zero"
        assert _score_vision(None) is None, \
            "a gateway that could not answer is UNMEASURED — never a zero, or a blip becomes " \
            "a permanent assignment"
        # …and an unmeasured candidate can never win a role
        fast_bad = {"route": "a", "model": "m", "score": 0.4, "secs": 5}
        down = {"route": "b", "model": "m", "score": None, "secs": 1}
        assert best([down, fast_bad])["route"] == "a", "a candidate that never ran cannot win"
        assert best([down]) is None, "no measured candidate means no pick"
        assert best([fast_bad, {"route": "c", "model": "m", "score": 0.4, "secs": 1}])["route"] == "c", \
            "a tie is broken by the faster route"
        print("OK: measure.py — the scorers score, an unmeasured candidate never wins, and a tie "
              "goes to the faster route")
        raise SystemExit(0)

    roles = a.role or sorted(TRIALS)
    print(f"measuring {len(roles)} role(s): {', '.join(roles)}"
          + ("  [dry run]" if a.dry_run else ""))
    for r in roles:
        print(f"\n== {r}")
        table, picks = measure([r], dry=a.dry_run)
        if not a.dry_run:
            for role, rows, win in table:
                if win:
                    print(f"  -> pick: {win['route']}:{win['model']}  (score {win['score']:.2f})")
    if not a.dry_run:
        print(f"\nwrote {PICKS}")
        print("agents.chat(role=...) now leads with these; the rest of the order stays behind.")
