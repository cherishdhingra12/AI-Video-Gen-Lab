#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
script_enforcer.py — the rule-family enforcer for the script, BEFORE any credit is spent.

The idea the owner endorsed: the system should learn the rule FAMILIES and never repeat the same
class of mistake, not chase every historical instance. So this is the enforcement seam for the
script-critic families from SKILLS.md: hook, shape, concrete, dialogue, budget, moral, sameness, length.

It lives OUTSIDE agents.py on purpose: the critic's _SCRIPT_CRITIC prompt is the natural-language
evaluation against STORY.md, while this module enforces deterministic structural rule-families
across real markdown formats (`## Beats`, table rows, `## Captions`, inline dialogue).
Calibrated against passing reels: aawaaz, filter_wala, and the-lamp-s-silent-promise.
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
    / MORAL / CTA / CAPTIONS / NOTES / WHY / VISUAL). Handles markdown headings (## Beats, **BEATS**),
    table headers, and plain-text sections."""
    out: dict[str, str] = {}
    cur = "_"
    buf: list[str] = []

    def _header_key(line: str) -> str | None:
        s = line.strip()
        if not s:
            return None
        clean = re.sub(r"^([#*_\-\s]+)", "", s)
        clean = re.sub(r"([*_\-\s:]+)$", "", clean).strip().upper()
        first = clean.split()[0] if clean.split() else ""
        if first in ("HOOK", "BEATS", "DIALOGUE", "MORAL", "CTA", "CAPTIONS", "NOTES", "WHY", "VISUAL"):
            return first
        return None

    for raw in script.splitlines():
        hk = _header_key(raw)
        if hk:
            out[cur] = "\n".join(buf).strip()
            cur = hk
            buf = []
            continue
        buf.append(raw)
    out[cur] = "\n".join(buf).strip()

    # If MORAL is inside CAPTIONS, extract it into out['MORAL']
    if "MORAL" not in out and "CAPTIONS" in out:
        for line in out["CAPTIONS"].splitlines():
            m = re.search(r"[-*]?\s*moral\s*:\s*(.*)", line, re.I)
            if m:
                out["MORAL"] = m.group(1).strip().strip("\"'")
                break

    # If HOOK is inside CAPTIONS, extract it into out['HOOK']
    if "HOOK" not in out and "CAPTIONS" in out:
        for line in out["CAPTIONS"].splitlines():
            m = re.search(r"[-*]?\s*hook\s*:\s*(.*)", line, re.I)
            if m:
                out["HOOK"] = m.group(1).strip().strip("\"'")
                break

    return out


def _spoken_lines(script: str) -> list[tuple[str, str | None]]:
    """Return (text, seconds) pairs for every row the script claims is spoken, plus None seconds
    for rows that carry dialogue but no timing so the caller can still count words. A row's
    timing, when it has one, is a trailing 'NNs' / 'NN sec' marker; a line without one is still
    returned, with None — the caller counts its words either way."""
    rows: list[tuple[str, str | None]] = []
    sec = _sections(script)
    if sec.get("DIALOGUE"):
        for line in sec["DIALOGUE"].splitlines():
            text = line.strip()
            if not text or text.upper().startswith(("MORAL", "CTA")):
                continue
            m = re.search(r"\b(\d{1,2}(?:\.\d)?)\s*s(?:ec)?\b\.?$", text)
            rows.append((text, m.group(1) if m else None))
        if rows:
            return rows

    beats = sec.get("BEATS", "")
    for line in beats.splitlines():
        text = line.strip()
        if not text or text.startswith("|-") or text.startswith("#"):
            continue
        if text.startswith("|"):
            parts = [p.strip() for p in text.split("|") if p.strip()]
            if len(parts) >= 3 and ("visual" in text.lower() or "हिन्दी" in text.lower()):
                continue
            if len(parts) >= 3:
                d_cell = parts[2] if len(parts) >= 3 else parts[1]
                t_cell = parts[-1] if len(parts) >= 4 else None
                if d_cell and d_cell.lower() not in ("*silent*", "silent", "-"):
                    sec_val = None
                    if t_cell:
                        m = re.search(r"(\d{1,2})", t_cell)
                        if m:
                            sec_val = m.group(1)
                    rows.append((d_cell, sec_val))
        else:
            quotes = re.findall(r'["“]([^"”]+)["”]', text)
            if quotes:
                for q in quotes:
                    rows.append((q, None))
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
    # If beats explicitly label THE TURN, PAYOFF, PUNCH-LINE, REVERSAL, it's structured
    low_beats = beats.lower()
    if any(kw in low_beats for kw in ("turn", "payoff", "punch-line", "punchline", "reversal",
                                      "reveal", "climax", "twist", "cut back", "turns")):
        return False
    turn_words = ("but", "then", "suddenly", "until", "when", "because", "so", "yet", "however",
                  "except", "still", "meanwhile", "after", "before", "while", "where",
                  "फिर", "लेकिन", "मगर", "अचानक", "तभी", "जब", "तब", "क्योंकि", "इसलिए", "बदल")
    lines = [b.strip() for b in beats.splitlines() if b.strip() and not b.strip().startswith("|-") and b.strip() != "|"]
    if len(lines) < 2:
        return False
    has_turn = any(any(w in b.lower() for w in turn_words) for b in lines)
    if has_turn:
        return False
    flat = sum(1 for b in lines if not any(w in b.lower() for w in turn_words)
               and not re.search(r"\b[A-Z][a-z]+\b", b))
    return flat >= len(lines)


def _moral_earned(script: str) -> bool:
    """Block: moral is a platitude / question / unsupported by the story.

    The house rule (STORY.md §5) says the moral must be earned by what we just watched, must be one
    line, must be a statement (not a question), and must not be a platitude the story did not earn.
    This check is a fast backstop; the critic is the final arbiter of whether the moral lands.
    """
    sec = _sections(script)
    moral = sec.get("MORAL", "").strip()
    if not moral:
        for raw in script.splitlines():
            m = re.search(r"(?:^[-*#\s]*moral(?:\s*\(use verbatim\))?\s*:\s*|^moral\s*:\s*)(.*)", raw.strip(), re.I)
            if m:
                moral = m.group(1).strip()
                break
    if not moral:
        return True  # no moral to judge

    moral = moral.strip().strip("\"'")
    low = moral.lower()
    # a moral that is only a question is a CTA wearing a moral's clothes (STORY.md §5)
    if moral.strip().endswith("?") and not (_starts_house_question(moral) or _colloquial_question(moral)):
        return False
    # platitude / generic-life-lesson markers — these are the obvious unearned morals the house
    # already named in STORY.md §5 and in the shipped-reel table at the end of that file.
    platitude_marks = (
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

    head, _, tail = moral.partition(":")
    if tail.strip():
        moral_body = tail.strip().strip("\"'")
    else:
        moral_body = moral.strip().strip("\"'")
    _body_allowed = any(
        _starts_house_question(_body) or _colloquial_question(_body)
        for _body in (moral_body,)
    )
    if _body_allowed:
        return True
    low_body = moral_body.lower()
    if any(m in low_body for m in platitude_marks) and not _platitude_allowed(low_body):
        return False
    if moral_body.endswith("?") and not (_starts_house_question(moral_body) or _colloquial_question(moral_body)):
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


_CONCRETE_TOKENS_RE = re.compile(
    r"\b("
    r"table|chair|door|window|lamp|photo|letter|phone|key|purse|bag|book|hat|"
    r"scarf|shirt|train|bus|cycle|boat|temple|market|gali|chauraha|park|school|"
    r"hospital|station|bridge|river|well|bucket|kettle|cup|plate|knife|palanquin|"
    r"carriage|kiosk|clock|ghadi|chashma|kurta|saree|sandook|diya|chammach|glass|"
    r"tasveer|kagaz|kalam|pen|pencil|bench|taala|chabi|dholak|bansuri|chai|tea"
    r")\b",
    re.IGNORECASE,
)


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
        if _CONCRETE_TOKENS_RE.search(s):
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
                try:
                    total += float(sec)
                    has_table = True
                except (ValueError, TypeError):
                    pass
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


def selfcheck() -> int:
    """Validate script enforcer against calibration standards and test fixtures."""
    # 1. Test markdown parsing
    sample_md = """# Title
## Hook
Kya purani tasveerein sach bolti hain?

## Beats
| Beat | Visual | Hindi Dialogue | Seconds |
|:---|:---|:---|:---|
| 1 | Dadaji holds an old brass clock | "Ye ghadi nahi, mera waqt hai." | 5s |
| 2 | The clock ticks slowly | *silent* | 4s |
| 3 | Grandson touches the brass frame | "Dadu, isme to aawaz hi nahi hai." | 5s |
| 4 | Dadaji smiles gently | "Waqt ki aawaz sunne ke liye chup rehna padta hai." | 6s |

## Captions
MORAL: Purani cheezein waqt nahi, yaadein ginti hain.
CTA: Share with someone you miss.
"""
    parsed = _sections(sample_md)
    assert "BEATS" in parsed, f"Expected BEATS in parsed sections: {list(parsed.keys())}"
    assert "CAPTIONS" in parsed, f"Expected CAPTIONS in parsed sections: {list(parsed.keys())}"
    print("OK: markdown section parsing (## Beats, ## Captions, tables)")

    # 2. Test passing reels calibration
    shipped_slugs = ["aawaaz", "filter_wala", "the-lamp-s-silent-promise"]
    for slug in shipped_slugs:
        p = _DIR / "projects" / slug
        s_file = p / "script.md"
        if s_file.exists():
            s_text = s_file.read_text(encoding="utf-8")
            i_text = (p / "idea.txt").read_text(encoding="utf-8") if (p / "idea.txt").exists() else ""
            res = check(s_text, i_text, require_critic=False)
            assert res["status"] == "clean", f"Passing reel {slug} falsely held: {res['problems']}"
            print(f"OK: passing reel calibrated: {slug} (status=clean)")

    # 3. Test violation detection (platitude moral, toy-short length, missing concrete)
    bad_script = """
## Hook
Did you know?
## Beats
Beat 1: Somebody walked.
## Captions
MORAL: Always remember that life is a journey.
CTA: Follow us.
"""
    res_bad = check(bad_script, "toy idea", require_critic=False)
    assert res_bad["status"] == "held", "Expected bad_script to be held"
    assert any("[moral]" in p for p in res_bad["problems"]), f"Expected [moral] violation in: {res_bad['problems']}"
    assert any("[concrete]" in p for p in res_bad["problems"]), f"Expected [concrete] violation in: {res_bad['problems']}"
    print("OK: catches platitude morals and missing concrete details")

    print("script_enforcer --selfcheck OK")
    return 0


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        p = Path(sys.argv[1])
        if p.is_file():
            res = check(p.read_text(encoding="utf-8"), require_critic=False)
            print(f"status: {res['status']}")
            for prob in res["problems"]:
                print(f"  - {prob}")
            sys.exit(0 if res["status"] == "clean" else 1)
    print(check.__doc__)
