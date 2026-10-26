"""
script_enforcer.py — the rule-family enforcer for the script, BEFORE any credit is spent.

The idea the owner endorsed: the system should learn the rule FAMILIES and never repeat the same
class of mistake, not chase every historical instance. So this is the enforcement seam for the
script-critic families from SKILLS.md: hook, shape, concrete, dialogue, budget, moral, sameness, length.

It lives OUTSIDE agents.py on purpose: the critic's _SCRIPT_CRITIC prompt is the natural-language
rubric the model already knows, and this module is the CODE that refuses to spend credits when the
script violates that rubric in a way the house treats as a block. One place for the rule, two places
for the protection: the prompt keeps the critic honest; this keeps the engine honest when the critic
is down or the run is being driven by something that does not call the critic.

Contract:
  script_enforcer.check(script, idea, shipped=...) -> dict(status='clean'|'held', problems=[...])
  * 'held' means DO NOT SPEND CREDITS on this script yet — it must be rewritten to the house standard.
  * 'clean' means this enforcer found no block it owns (other checks still run as usual).

This is the enforcement for the script rule FAMILIES. It is NOT a replacement for the critic's full
judgment, and it does NOT re-implement every STORY.md sentence. It owns the FAMILIES the house agreed
should be enforced before credit spend, in code, so the enforcement survives a model swap and a missing
STORY.md as much as possible. Where it cannot decide, it returns 'clean' and lets the gate catch it.

Design notes the owner should read before touching:
  * ABSOLUTE MINIMUM. This blocklist is deliberately tiny. The rule FAMILIES are real; the code path is
    only the part that must refuse a credit spend. Everything else stays in STORY.md and the critic.
  * FAIL-OPEN. Missing STORY.md, missing shipped shapes, and a script that is too short to judge are
    NOT defects here — they are 'cannot decide yet', and the gate is still the gate.
  * TAGGED FINDINGS. Every problem carries a [tag] so the same class of mistake can be counted across
    reels, the way the critic already does. A problem without a tag is not a finding the enforcer owns.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# ---- bought-in pieces, all already in this project ----

# The script critic is the natural-language authority for the writing standard. This enforcer does not
# re-implement its rubric; it hard-blocks only the classes the house agreed must be enforced before
# credit spend. When the critic is reachable, that is the richer reader; when it is not, this is the
# backstop that still stops the worst repeat-class mistakes.
try:
    from agents import _script_complete, _shipped_shapes, critique_script  # noqa: F401
except Exception:  # noqa: N816 — fail-open; the enforcer degrades to code-only checks
    _script_complete = None
    _shipped_shapes = None
    critique_script = None

# STORY.md is the actual writing standard. Read it when present; absent is fail-open.
_STORY = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "STORY.md")
_STORY_TEXT = ""
if _STORY.exists():
    try:
        _STORY_TEXT = _STORY.read_text(encoding="utf-8").strip()
    except OSError:
        _STORY_TEXT = ""


# ---- internal helpers ----

def _sections(script: str) -> dict:
    """Which script sections are present. The enforcer only judges from sections it can see."""
    present = {}
    for name in ("HOOK", "MORAL", "BEATS", "LOGLINE", "VISUAL STYLE"):
        if name in script.upper():
            present[name] = True
    return present


def _spoken_lines(script: str) -> list:
    """Extract the script's spoken lines with their claimed seconds, from a BEATS table if the
    script has one and looks tabular. Returns [(text, seconds_or_None), ...]."""
    lines = []
    if "BEATS" not in script.upper():
        return lines
    in_beats = False
    for raw in script.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.upper().startswith("BEATS"):
            in_beats = True
            continue
        if in_beats and (s.upper().startswith("MORAL") or s.upper().startswith("CTA")):
            break
        if in_beats and s.startswith("|"):
            # common pipe table: | N | action | dialogue | seconds |
            cells = [c.strip() for c in s.split("|")]
            cells = [c for c in cells if c]
            if len(cells) >= 3:
                text = cells[-2].strip()
                sec = cells[-1].strip()
                try:
                    sec = float(sec)
                except ValueError:
                    sec = None
                # drop pure action cells
                if text and not text.startswith("*silent*"):
                    text = text.strip("*")
                    lines.append((text, sec))
    return lines


def _word_count(text: str) -> int:
    return len(text.split())


def _is_preface_or_contents(script: str) -> bool:
    """Block: hook is a preface / contents page, not a question about a person."""
    if "HOOK" not in script.upper():
        return False
    # find the hook block
    lines = script.splitlines()
    hook_lines = []
    on = False
    for raw in lines:
        s = raw.strip()
        if s.upper().startswith("HOOK"):
            on = True
            hook_lines.append(s)
            continue
        if on:
            if s.upper().startswith(("LOGLINE", "VISUAL", "BEATS", "MORAL", "CTA")):
                break
            hook_lines.append(s)
    text = " ".join(hook_lines)
    # a hook that is only a sentence introducing the reel, or a list of beats, is a preface
    preface_marks = (
        "this is a story about",
        "this reel is about",
        "the story of",
        "in this film",
        "a short film about",
        "follows",
        "whose life",
        "this is the story",
    )
    low = text.lower()
    if any(m in low for m in preface_marks):
        return True
    # a hook that is a declared contents list
    if "parts" in low and ("part 1" in low or "part 1:" in low or "1." in low):
        return True
    return False


def _beats_swappable(script: str) -> bool:
    """Block: the beats can be swapped with no loss — no escalation, no turn."""
    if "BEATS" not in script.upper():
        return False
    lines = _spoken_lines(script)
    if not lines:
        return False
    # crude: if no beat has a spoken line longer than a bare word and no beat is obviously a turn,
    # the beat order carries no story weight. This is a backstop, not the critic.
    has_dialogue = any(_word_count(t) >= 2 for t, _ in lines)
    has_silent = any(t.strip().startswith("*silent*") for t, _ in lines)
    if not has_dialogue and not has_silent:
        return True
    # a turn is usually in the last 40% and is a change of state, not just another beat
    tail = lines[int(len(lines) * 0.6):]
    has_turn = any(_word_count(t) >= 4 for t, _ in tail)
    return not has_turn


def _moral_earned(script: str) -> bool:
    """Block: moral is a platitude / question / unsupported by the story."""
    if "MORAL" not in script.upper():
        return False
    mstart = None
    for i, raw in enumerate(script.splitlines()):
        if raw.strip().upper().startswith("MORAL"):
            mstart = i
            break
    if mstart is None:
        return False
    moral = " ".join(
        raw.strip() for raw in script.splitlines()[mstart: mstart + 3] if raw.strip()
    )
    low = moral.lower()
    platitude_marks = (
        "always",
        "never forget",
        "life is",
        "life's",
        "in the end",
        "at the end of the day",
        "we all",
        "we should",
        "we must",
        "we need to",
        "every",
        "it is said",
        "as the",
        "honesty is",
        "love is",
        "family is",
        "time is",
        "hope is",
        "the real",
        "true",
        "?",
    )
    if any(m in low for m in platitude_marks):
        return False
    # a moral that is only a question is not a moral
    if moral.strip().endswith("?"):
        return False
    return True


def _concrete_present(script: str) -> bool:
    """Block: no specific, nameable concrete detail survives (count, date, brand, name, object)."""
    # crude proxy: a concrete script names some countable, dated, branded, or named thing.
    concrete_patterns = (
        r"\b\d+\s",
        r"\b19\d{2}\b",
        r"\b20\d{2}\b",
        r"\b\d+\s*(?:year|GB|kg|km|m|cm|litre|rupee|r\s)?\b",
        r"\b[A-Z][a-z]+ [A-Z][a-z]+\b",  # a capitalized name pair
        r"\b[a-z]+ [A-Z][a-z]+\b",
        r"\b#\d+\b",
        r"\b\d{1,3}(,\d{3})+\b",
        r"\bpackets?\b",
        r"\bbottles?\b",
        r"\bjars?\b",
        r"\bbag\b",
        r"\b register\b",
        r"\b chair\b",
        r"\b phone\b",
        r"\b table\b",
        r"\b basket\b",
        r"\b flag\b",
        r"\b register\b",
    )
    if any(re.search(p, script) for p in concrete_patterns):
        return True
    return False


def _dialogue_fit(script: str) -> list:
    """Block: spoken lines do not fit their seconds. Returns problems, [] when fine."""
    problems = []
    for text, sec in _spoken_lines(script):
        if sec and sec > 0:
            words = _word_count(text)
            # spoken Hindi averages roughly 2.2 w/s; 3s clip ~= 6-7 words
            if words > max(6, int(sec * 2.6)):
                problems.append(
                    f"[dialogue] {text!r} is {_word_count(text)} words in {sec:.1f}s — "
                    f"too long to perform at a natural spoken rate"
                )
    return problems


def _budget_fit(script: str) -> list:
    """Block: a line exceeds the time budget implied by its beat (when the script states seconds)."""
    problems = []
    for text, sec in _spoken_lines(script):
        if sec and sec > 0:
            words = _word_count(text)
            # a 3-4s beat carrying 9-10 words is the classic over-budget case
            if sec <= 4 and words > 8:
                problems.append(
                    f"[budget] {text!r} is {words} words for {sec:.1f}s — over the beat budget"
                )
    return problems


def _length_ok(script: str, idea: str) -> bool:
    """Block: the script's intended length is far too short for a reel.

    Very crude: if the script declares a total runtime in seconds and it is under ~20s, or the
    implied reel is obviously toy-short, hold it. A reel under ~20s is not a reel, it is a clip.
    """
    # count declared beat seconds if tabular
    total = 0.0
    has_table = False
    if "BEATS" in script.upper():
        for text, sec in _spoken_lines(script):
            if sec:
                has_table = True
                total += sec
    if has_table and total < 20:
        return False
    return True


def _sameness_danger(script: str, shipped: list) -> bool:
    """Block: this script is clearly repeating a shipped reel's structure/motif/moral.

    This enforcer does NOT own the full sameness rule — that is the critic's and the house's. But it
    does own the cheap, definite case: the new script's moral line is the SAME as a shipped reel's
    moral, word for word. A verbatim-repeat moral is the easiest repeat-class to catch before credit
    spend, and the house agreed the moral must be fresh per reel.
    """
    if not shipped:
        return False
    mstart = None
    for i, raw in enumerate(script.splitlines()):
        if raw.strip().upper().startswith("MORAL"):
            mstart = i
            break
    if mstart is None:
        return False
    moral = " ".join(
        raw.strip() for raw in script.splitlines()[mstart: mstart + 2] if raw.strip()
    ).lower()
    for s in shipped:
        sm = (s.get("moral") or "").lower()
        if sm and moral == sm:
            return True
        # close paraphrase is not a hard block here; let the critic decide
    return False


# ---- public API ----

def check(
    script: str,
    idea: str = "",
    shipped: list = None,
    *,
    require_critic: bool = False,
) -> dict:
    """Enforce the script rule FAMILIES before credit spend.

    Returns:
      {'status': 'clean'|'held', 'problems': [...], 'by': 'script-enforcer'}

    When require_critic is True AND the critic is reachable, the enforcer ALSO runs the critic once
    and treats a non-empty critic problem list as a hold. This is the path that makes the enforcer
    useful immediately: the critic's judgment becomes a hard pre-credit block rather than a record in
    candidate_lessons.
    """
    problems: list = []
    sections = _sections(script)

    # --- FAMILY: hook ---
    if _is_preface_or_contents(script):
        problems.append(
            "[hook] the hook reads as a preface or contents, not a question about a person"
        )

    # --- FAMILY: shape ---
    if _beats_swappable(script):
        problems.append(
            "[shape] the beats carry no escalation or turn — they can be swapped with no loss"
        )

    # --- FAMILY: moral ---
    if "MORAL" in script.upper() and not _moral_earned(script):
        problems.append(
            "[moral] the moral reads as a platitude, a question, or an unearned line — "
            "write a moral this story alone could say"
        )

    # --- FAMILY: concrete ---
    if not _concrete_present(script):
        problems.append(
            "[concrete] no specific, nameable concrete detail survives — one physical thing "
            "the viewer could point at after watching"
        )

    # --- FAMILY: dialogue (word count vs seconds) ---
    problems.extend(_dialogue_fit(script))

    # --- FAMILY: budget ---
    problems.extend(_budget_fit(script))

    # --- FAMILY: length ---
    if not _length_ok(script, idea):
        problems.append(
            "[length] the reel is too short to land a story with a moral — "
            "expand the beats to a reel-length film"
        )

    # --- FAMILY: sameness (cheap, definite case only) ---
    if _sameness_danger(script, shipped or (_shipped_shapes() if _shipped_shapes else []) or []):
        problems.append(
            "[sameness] this reel's moral repeats a shipped reel verbatim — "
            "the moral must be fresh for this reel"
        )

    # --- OPTIONAL: the real critic, when reachable ---
    if require_critic and critique_script is not None:
        try:
            crit = critique_script(script, idea, priors=(shipped or []) or None)
            extra = [p for p in (crit.get("problems") or []) if str(p).strip()]
            if extra:
                problems.extend(extra)
        except Exception:  # noqa: BLE001 — critic down is not a block for the enforcer
            pass

    status = "held" if problems else "clean"
    return {"status": status, "problems": problems, "by": "script-enforcer"}


def enforce_before_credit(
    script: str,
    idea: str = "",
    shipped: list = None,
    *,
    fail_hard: bool = True,
) -> dict:
    """The seam the engine calls right before it would spend a generation credit on a script.

    When fail_hard is True and the script is held, this raises ScriptHeld so the caller does not
    spend credits on a script the house has already named a repeat-class mistake for.

    Returns the same shape as check(); when fail_hard is False, 'held' is returned rather than
    raised, so a caller doing a dry-run can read the block without stopping.
    """
    result = check(script, idea, shipped)
    if result["status"] == "held" and fail_hard:
        raise ScriptHeld(result["problems"])
    return result


class ScriptHeld(Exception):
    """A script the house enforcer refused to spend credits on."""

    def __init__(self, problems: list):
        self.problems = list(problems)
        super().__init__("script held by script_enforcer — do not spend credits: " + "; ".join(problems[:6]))


if __name__ == "__main__":
    # smoke test against the planted weak script in measure.py's test battery, plus a clean one.
    import json

    _TEST_DIR = os.path.dirname(os.path.abspath(__file__))
    _MEASURE = os.path.join(_TEST_DIR, "measure.py")
    _weak = ""
    if os.path.exists(_MEASURE):
        _g = {}
        for line in open(_MEASURE, encoding="utf-8"):
            if line.startswith(" _SCRIPT = ") or line.startswith("_SCRIPT = "):
                exec(line, _g)
                _weak = _g.get("SCRIPT", "")
                break
    if not _weak:
        _weak = (
            "# दादाजी की कहानी\n\n"
            "IDEA: an old man's memories\n\n"
            "MEDIUM: 3-D ANIMATION — stylized animated-feature look, rounded shapes, soft global "
            "illumination, cinematic lighting.\n\n"
            "LOGLINE: an old man remembers the people he failed to keep.\n\n"
            "VISUAL STYLE: warm amber, 3-D animation, slow gentle camera.\n\n"
            "BEATS:\n"
            "| 1 | Dadaji sits alone | *silent* | 4 |\n"
            "| 2 | a photo | *silent* | 4 |\n"
            "| 3 | he smiles | यादें हमेशा साथ रहती हैं | 4 |\n"
            "MORAL: यादें हमेशा साथ रहती हैं\n\n"
            "CTA: standardsunrisefoundation.org\n"
        )

    _clean = (
        "# दादाजी की कहानी\n\n"
        "IDEA: an old man's memories\n\n"
        "MEDIUM: 3-D ANIMATION — stylized animated-feature look, rounded shapes, soft global "
        "illumination, cinematic lighting.\n\n"
        "LOGLINE: an old man finds one voice he thought the world had lost.\n\n"
        "VISUAL STYLE: warm amber, 3-D animation, slow gentle camera.\n\n"
        "BEATS:\n"
        "| 1 | a phone held low records him mending a chair | क्या तुम्हें यह आवाज़ याद है? | 8 |\n"
        "| 2 | the family listens to his own voice | *silent* | 5 |\n"
        "| 3 | he finishes the song he had left unfinished | पिता की आवाज़ कभी नहीं जाती | 7 |\n"
        "MORAL: पिता की आवाज़ कभी नहीं जाती\n\n"
        "CTA: standardsunrisefoundation.org\n"
    )

    print("=== weak script (should be held by the FAMILY checks) ===")
    r = check(_weak, "an old man's memories")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print()
    print("=== clean script (should be clean to the FAMILY checks) ===")
    r2 = check(_clean, "an old man's memories")
    print(json.dumps(r2, ensure_ascii=False, indent=2))
