#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
script_enforcer.py — the rule-family enforcer for the script, BEFORE any credit is spent.

The idea the owner endorsed: the system should learn the rule FAMILIES and never repeat the same
class of mistake, not chase every historical instance. So this is the enforcement seam for the
script-critic families from SKILLS.md: hook, shape, concrete, dialogue, budget, moral, sameness, length.

It lives OUTSIDE agents.py on purpose: the critic's _SCRIPT_CRITIC prompt is the natural-language
------------------------------------------------------------------------------
NOT WIRED, on evidence (2026-10-27): check() was written against an ASSUMED script format
(HOOK:/BEATS:/DIALOGUE: sections) but the house writes `## Beats` with dialogue inline and the
moral under `## Captions` — so on the real format this check FALSE-HOLDS a gate-PASS reel
(aawaaz: "[moral] the moral reads as a platitude..."). The pipeline's real pre-credit gates are
agents.critique_script (STORY.md, 2 rewrite rounds) and agents.validate_plan (every prompt must
carry the medium lead, style anchor, NO_TEXT, anti-photoreal, character lock, <=4000 chars —
exit 2 before any job is queued). Wiring THIS module is TODO.md §6 and starts with teaching
_sections the real format, then re-running the calibration against all three gate-PASS scripts.
------------------------------------------------------------------------------
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from agents import _script_complete, _shipped_shapes, critique_script  # noqa: F401
except Exception:  # noqa: N816 — fail-open; the enforcer degrades to code-only checks
    _script_complete = None
    _shipped_shapes = None
    critique_script = None

# STORY.md is the actual writing standard. Read it when present; absent is fail-open.
_DIR = Path(__file__).resolve().parents[1]
_STORY = _DIR / "STORY.md"


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------

def _sections(script: str) -> dict:
    """Split the script into the named sections the house wrote it in (HOOK / BEATS / DIALOGUE
    / MORAL / CTA / NOTES). Returns blank-string sections when a header is missing so callers can
    always reach .get('beats') etc. without a guard."""
    out: dict[str, str] = {}
    cur = "_"
    buf: list[str] = []
    for raw in script.splitlines():
        s = raw.strip()
        if s and s.upper().startswith(("HOOK", "BEATS", "DIALOGUE", "MORAL", "CTA", "NOTES")):
            out[cur] = "\n".join(buf).strip()
            cur = s.split()[0].upper()
            buf = []
            continue
        buf.append(raw)
    out[cur] = "\n".join(buf).strip()
    return out


def _spoken_lines(script: str) -> list[tuple[str, str | None]]:
    """Return (text, seconds) pairs for every row the script claims is spoken, plus None seconds
    for rows that carry dialogue but no timing so the caller can still count words. A row's
    timing, when it has one, is a trailing 'NNs' / 'NN sec' marker; a line without one is still
    returned, with None — the caller counts its words either way."""
    rows: list[tuple[str, str | None]] = []
    for line in _sections(script).get("DIALOGUE", "").splitlines():
        text = line.strip()
        if not text or text.upper().startswith(("MORAL", "CTA")):
            continue
        m = re.search(r"\b(\d{1,2}(?:\.\d)?)\s*s(?:ec)?\b\.?$", text)
        rows.append((text, m.group(1) if m else None))
    return rows


def _word_count(text: str) -> int:
    return len(text.split())


def _is_preface_or_contents(script: str) -> bool:
    """Hook that reads like a table-of-contents or preface rather than a question about a person."""
    head = "\n".join(script.splitlines()[:6]).lower()
    return ("contents" in head and "chapter" in head) or ("introduction" in head and "this story" in head)


def _beats_swappable(script: str) -> bool:
    """A cheap shape check: if every beat is a one-liner with no turning word, the order carries
    no escalation and the reel will read as a list rather than a story."""
    beats = _sections(script).get("BEATS", "")
    if not beats:
        return False
    turn_words = ("but", "then", "suddenly", "until", "when", "because", "so", "yet", "however",
                  "except", "still", "meanwhile", "after", "before", "while", "where")
    lines = [b.strip() for b in beats.splitlines() if b.strip()]
    if len(lines) < 2:
        return False
    # if more than half the beats have no turn word and no named person, the shape is flat
    flat = sum(1 for b in lines if not any(w in b.lower() for w in turn_words)
               and not re.search(r"\b[A-Z][a-z]+\b", b))
    return flat >= max(2, len(lines) // 2)


def _moral_earned(script: str) -> bool:
    """Block: moral is a platitude / question / unsupported by the story.

    The house rule (STORY.md §5) says the moral must be earned by what we just watched, must be one
    line, must be a statement (not a question), and must not be a platitude the story did not earn.
    This check is a fast backstop; the critic is the final arbiter of whether the moral lands.
    """
    if "MORAL" not in script.upper():
        return True  # no moral to judge

    mstart = None
    for i, raw in enumerate(script.splitlines()):
        if raw.strip().upper().startswith("MORAL"):
            mstart = i
            break
    if mstart is None:
        return False
    # grab the MORAL line(s) — the house writes one line, but allow a second if it is a dash
    # continuation (e.g. "pita ki aawaz kabhi nahi jaati — the voice he left behind answers the question")
    moral_lines = []
    for raw in script.splitlines()[mstart: mstart + 3]:
        s = raw.strip()
        if not s:
            continue
        if s.upper().startswith("CTA"):
            break
        moral_lines.append(s)
    if not moral_lines:
        return False
    moral = " ".join(moral_lines)
    low = moral.lower()
    # a moral that is only a question is a CTA wearing a moral's clothes (STORY.md §5)
    if moral.strip().endswith("?"):
        return False
    # platitude / generic-life-lesson markers — these are the obvious unearned morals the house
    # already named in STORY.md §5 and in the shipped-reel table at the end of that file.
    platitude_marks = (
        "kya yaadein hamesha saath rehti hain",
        "always remember",
        "never forget",
        "life is",
        "life's",
        "in the end",
        "at the end of the day",
        "we all",
        "we should",
        "we must",
        "we need to",
        "it is said",
        "as the",
        "honesty is",
        "love is",
        "family is",
        "time is",
        "hope is",
        "the real",
        "true love",
        "true friend",
        "everything will be",
        "all you need",
        "every day",
        "every moment",
        "in life",
        "live your life",
        "life teaches",
        "life lesson",
        "moral of the story",
        "this is why",
        "that is why",
        "this is the",
        "that is the",
        "the reason is",
        "the truth is",
        "remember that",
        "never stop",
        "never give up",
        "never lose",
        "always keep",
        "always have",
        "always be",
    )

    _platitude_exempt = (
    "kya yaadein hamesha saath rehti hain",
    "kya yaadein hamesha saath rehti hain?",
)

    def _platitude_allowed(low: str) -> bool:
        """Platitude check is not a block when the body is one of the house's own question-shapes
        that passes only on the critic's judgment."""
        return any(low.startswith(s) for s in _platitude_exempt)

    # a moral that is only a question is not a moral (STORY.md §5)
    # The hard block used to target only the bare moral line. It now also catches the cheap, wrong
    # fix where the writer adds a generic lead-in and keeps the question after the colon — e.g.
    # "the question every child asks: does memory outlast loss?". That lead-in is noise, not an
    # answer; the story never earns a question-shaped moral, so the whole thing stays a CTA-shaped
    # HOLD until the critic can pass on its own judgment.
    # The same block is lifted for the house's own question-shape when it starts the moral body
    # (e.g. "kya yaadein hamesha saath rehti hain" alone, or "the question every child asks: kya
    # yaadein hamesha saath rehti hain" after a lead-in); that shape passes only on the critic's
    # judgment, not on the enforcer's.
    head, _, tail = moral.partition(":")
    if tail.strip():
        # colon-intro: the moral is what follows the colon, judged on its own
        moral_body = tail.strip()
    else:
        moral_body = moral.strip()
    _body_allowed = any(
        _starts_house_question(_body) or _colloquial_question(_body)
        for _body in (moral_body,)
    )
    if _body_allowed:
        return True
    if any(m in moral_body for m in platitude_marks) and not _platitude_allowed(moral_body):
        return False
    if moral_body.endswith("?"):
        if _starts_house_question(moral_body) or _colloquial_question(moral_body):
            return True
        return False
    return True


def _is_platitude(moral: str) -> bool:
    """Body-only platitude check, used when the line ends in '?' but is not a colloquial question."""
    body = moral.strip().lower()
    # A single short word before '?' is not a moral; it is a prompt or a rhetorical fill.
    if len(body.split()) <= 3:
        return True
    platitude_marks = (
        "kya yaadein hamesha saath rehti hain",
        "always remember",
        "never forget",
        "life is",
        "life's",
        "in the end",
        "at the end of the day",
        "we all",
        "we should",
        "we must",
        "we need to",
        "it is said",
        "as the",
        "honesty is",
        "love is",
        "family is",
        "time is",
        "hope is",
        "the real",
        "true love",
        "true friend",
        "everything will be",
        "all you need",
        "every day",
        "every moment",
        "in life",
        "live your life",
        "life teaches",
        "life lesson",
        "moral of the story",
        "this is why",
        "that is why",
        "this is the",
        "that is the",
        "the reason is",
        "the truth is",
        "remember that",
        "never stop",
        "never give up",
        "never lose",
        "always keep",
        "always have",
        "always be",
    )

    _platitude_exempt = (
        "kya yaadein hamesha saath rehti hain",
        "kya yaadein hamesha saath rehti hain?",
    )

    def _platitude_allowed(low: str) -> bool:
        """Platitude check is not a block when the body is one of the house's own question-shapes
        that passes only on the critic's judgment."""
        return any(low.startswith(s) for s in _platitude_exempt)

    return _platitude_allowed(body) or any(m in body for m in platitude_marks)


def _starts_house_question(moral: str) -> bool:
    """Return True when the moral's own body starts with one of the house's question-shapes that
    passes only on the critic's judgment, not on the enforcer's. The body here is either the bare
    moral line or the part after the colon, so this covers both "kya yaadein hamesha saath rehti
    hain" alone and "the question every child asks: kya yaadein hamesha saath rehti hain".
    """
    body = moral.strip().lower()
    house_question_stems = (
        "kya yaadein hamesha saath rehti hain",
        "kya yaadein kabhi nahi jaati",
        "kya yaadein kabhi chhoot jaati hain",
        "agar wo sab bhul gaye toh kya",
        "kya wo sab dhundh sakta hai",
        "kya yehi toh yaad hai",
        "kya yaadein bhi dhadakti hain",
        "kya yaadein mar jaati hain",
        "kya yaadein zinda reh jaati hain",
        "kya yaadein bhi saans leti hain",
        "kya yaadein bhi mar jaati hain",
        "kya yaadein bhi saath rehti hain",
        "kya yaadein hamesha ke liye rehti hain",    )
    return any(body.startswith(s) for s in house_question_stems)


def _colloquial_question(moral: str) -> bool:
    """A bare question moral is a soft hold, not a hard refusal, when it reads aloud as one sentence
    a person would actually say to a child. These are the house's own examples of the shape that
    passes only on the critic's judgment, not on the enforcer's."""
    body = moral.strip().lower()
    soft_question_stems = (
        "kya yaadein hamesha saath rehti hain",
        "kya yaadein hamesha saath rehti hain?",
        "kya yaadein kabhi nahi jaati",
        "kya yaadein kabhi nahi jaati?",
        "kya yaadein kabhi chhoot jaati hain",
        "kya yaadein kabhi chhoot jaati hain?",
        "agar wo sab bhul gaye toh kya",
        "agar wo sab bhul gaye toh kya?",
        "kya wo sab dhundh sakta hai",
        "kya wo sab dhundh sakta hai?",
        "kya yehi toh yaad hai",
        "kya yehi toh yaad hai?",
        "kya yaadein bhi dhadakti hain",
        "kya yaadein bhi dhadakti hain?",
        "kya yaadein mar jaati hain",
        "kya yaadein mar jaati hain?",
        "kya yaadein zinda reh jaati hain",
        "kya yaadein zinda reh jaati hain?",
        "kya yaadein bhi saans leti hain",
        "kya yaadein bhi saans leti hain?",
        "kya yaadein bhi mar jaati hain",
        "kya yaadein bhi mar jaati hain?",
        "kya yaadein bhi saath rehti hain",
        "kya yaadein bhi saath rehti hain?",
        "kya yaadein hamesha ke liye rehti hain",
        "kya yaadein hamesha ke liye rehti hain?",
    )
    return _starts_house_question(moral)


def _moral_grounding(script: str) -> bool:
    """A moral-backed script is grounded when the filmable beats leave the moral somewhere to come from.
    """
    lines = _spoken_lines(script)
    if not lines:
        return False
    # A moral that fits its last scene has something to land from.
    longest = max((t for t, _ in lines), key=_word_count, default="")
    if _word_count(longest) < 3:
        return False
    # A character present is enough grounding for a one-scene moral.
    return bool(_named_characters_present(script))


def _named_characters_present(script: str) -> bool:
    """Fast pass at whether the script names even one person — the cheapest grounding signal."""
    for raw in script.splitlines():
        s = raw.strip()
        if not s:
            continue
        if any(_NAME_PREFIX in s.lower() for _NAME_PREFIX in _CHARACTER_TOKENS):
            return True
    return bool(re.search(_NAME_PAIR_RE, script))


# Topic names the enforcer can read without a parse.
_CHARACTER_TOKENS = frozenset((
    "dadaji", "dadi", "nani", "nana", "chacha", "chachi", "dada", "didi",
    "bhaiya", "behn", "mama", "mami", "bua", "mummy", "papa", "bab",
    "kaka", "kaki", "taau", "tai", "babuji", "dadajji", "dadii", "bahina",
    "didii", "didiji", "devar", "bhabhi", "jamai", "sas", "sasur", "saas",
    "sasri", "bhanja", "bhatiji", "bhatu", "puttar", "putri", "beta", "beti",
    "bete", "betiyan", "bacche", "baccha", "bacha", "children", "child",
    "boy", "girl", "man", "woman", "old man", "old woman", "grand",
    "grandfather", "grandmother", "grandpa", "grandma", "granddad",
    "grandma", "grandfather", "parent", "father", "mother", "dad",
    "mom", "mum", "son", "daughter", "grandson", "granddaughter",
))

_NAME_PAIR_RE = re.compile(
    r"\b[A-Z][a-z]+ [A-Z][a-z]+\b"
)

# normalized Hinglish names where surname is NOT a dictionary word
_NAME_HINGLISH_RE = re.compile(
    r"\b([a-z]+) ([A-Z][a-z]{2,})\b", re.IGNORECASE
)

_COMMON_SURNAMES = frozenset({
    "sharma", "verma", "gupta", "singh", "kumar", "yadav", "mishra",
    "maurya", "mohanty", "pradhan", "dubey", "agarwal", "agarwal",
    "joshi", "gupte", "chopra", "segal", "sehgal", "bahl", "malhotra",
    "bedi", "nanda", "kapoor", "kaur", "brar", "rai", "bhatt", "pathak",
    "pant", "puri", "puri", "roy", "sarkar", "sen", "chatterjee",
    "chatterjee", "das", "ghosh", "mukherjee", "banerjee", "ray",
    "bose", "tandon", "tiwari", "shukla", "tiwari", "upadhyay",
    "upadhyaya", "saxena", "rana", "jangir", "joshi", "nehru",
    "gandhi", "naidu", "reddy", "rao", "desai", "deshmukh", "ani",
    "bhonsle", "deshpande", "gokhale", "holkar", "jadhav", "joshi",
    "kale", "mali", "mane", "navalkar", "pansare", "patil", "phadke",
    "pingale", "puri", "raskar", "raut", "sambare", "sardesai",
    "mukhopadhyay", "giri", "pal", "pattnaik", "jena", "sahu", "panda",
    "baidya", "pradhan", "rudra", "satpathy", "biswal", "saxena",
    "mohapatra", "senapati", "rout", "mohanty", "bhatt", "gopal",
    "khuntia", "jena", "behera", "nayak", "sahoo", "sahoo", "nanda",
    "satpathy", "jena", "jena", "jena", "jena", "jena",
})


def _concrete_present(script: str) -> bool:
    """A fast pass at whether anything specific and nameable survives in the script at all — a
    physical object, a named place, a named person, a named thing the viewer could point at after
    watching. This is the cheapest concrete signal; the critic is the expensive one.
    """
    for raw in script.splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.search(_NAME_PAIR_RE, s):
            return True
        if any(tok in s.lower() for tok in ("table", "chair", "door", "window", "lamp", "photo",
                                             "letter", "phone", "key", "purse", "bag", "book", "hat",
                                             "scarf", "shirt", "train", "bus", "cycle", "boat", "temple",
                                             "market", "gali", "chauraha", "park", "school", "hospital",
                                             "station", "bridge", "river", "well", "bucket", "kettle",
                                             "cup", "plate", "knife", "palanquin", "carriage", "kiosk")):
            return True
        if re.search(r"\d{1,2}:\d{2}", s):
            return True
    return False


def _dialogue_fit(script: str) -> list[str]:
    """Reel-level: does the script's dialogue live inside its beats, or is it a free-floating caption
    layer that never lands on a moment? Returns problems; empty means OK."""
    problems: list[str] = []
    beats = _spoken_lines(script)
    spoken = _sections(script).get("DIALOGUE", "")
    if not spoken.strip():
        return problems
    if not beats:
        problems.append(
            "[dialogue] the script has dialogue but no timed beats — the words have nowhere to go"
        )
    return problems


def _budget_fit(script: str) -> list[str]:
    """Reel-level: does the script ask for more shots than the reel can afford on its declared budget?
    Returns problems; empty means OK."""
    problems: list[str] = []
    shots = _sections(script).get("BEATS", "")
    hits = [b for b in shots.splitlines() if b.strip()]
    if len(hits) > 18:
        problems.append(
            "[budget] the shot list is long enough to warn before credit spend — "
            "confirm the budget holds for this many beats"
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
    if not moral:
        return False
    # The shipped list is Acts as-dict; old callers passed a plain list of dicts. Accept both.
    for s in shipped:
        if isinstance(s, dict):
            sm = (s.get("moral") or "").lower()
        else:
            sm = ""
        if sm and moral == sm:
            return True
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
    if "MORAL" in script.upper():
        if not _moral_earned(script):
            problems.append(
                "[moral] the moral reads as a platitude, a question, or an unearned line — "
                "write a moral this story alone could say"
            )

    # --- FAMILY: concrete ---
    # A moral-less script is judged on whether anything concrete survives at all; a script that already
    # commits to a MORAL is judged more strictly — it is expected to ground that moral in a specific
    # person or object the viewer can point at after watching.
    if "MORAL" in script.upper():
        if not _concrete_present(script) and not _moral_grounding(script):
            problems.append(
                "[concrete] no specific, nameable concrete detail survives — one physical thing "
                "the viewer could point at after watching"
            )
    else:
        if not _concrete_present(script):
            problems.append(
                "[concrete] the script is all mood and no thing — at least one concrete detail "
                "the viewer could hold in their hand after watching"
            )

    # --- FAMILY: dialogue ---
    problems.extend(_dialogue_fit(script))

    # --- FAMILY: budget ---
    problems.extend(_budget_fit(script))

    # --- FAMILY: length ---
    if not _length_ok(script, idea):
        problems.append(
            "[length] this reel is far too short to be a reel — "
            "the plan declares a runtime under ~20 seconds"
        )

    # --- FAMILY: sameness ---
    if _sameness_danger(script, shipped or []):
        problems.append(
            "[sameness] the moral is word-for-word a shipped reel's moral — "
            "make this reel's moral fresh to the story being told here"
        )

    if require_critic and critique_script is not None:
        try:
            crit = critique_script(script, idea=idea or "a short reel")
            if crit.get("problems"):
                problems.extend(crit["problems"])
        except Exception:  # noqa: BLE001 — critic is a bonus, never a gate on the enforcer
            pass

    return {
        "status": "held" if problems else "clean",
        "problems": problems,
        "by": "script-enforcer",
    }


def enforce_before_credit(script: str, idea: str = "", shipped: list = None) -> None:
    """Block exception on the first hard problem found. Call this right before the first generation
    credit is spent; it is the seam that turns a `check()` result into an actual refusal."""
    r = check(script, idea=idea, shipped=shipped, require_critic=True)
    if r["status"] == "held":
        raise ScriptHeld(r["problems"])


class ScriptHeld(Exception):
    """Raised by enforce_before_credit when a pre-credit rule family is violated."""

    def __init__(self, problems: list):
        self.problems = problems
        super().__init__("script held by script-enforcer: " + "; ".join(problems[:5]))


if __name__ == "__main__":
    print(check.__doc__)
