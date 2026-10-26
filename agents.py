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

# the automation package holds the shared pieces (this module lives at the repo root, so it is not
# importable as a package of its own) — same insert _brief used to do inline.
sys.path.insert(0, os.path.join(_DIR, "automation"))

import routerhealth  # noqa: E402  (remembers which gateway is walled — see chat())

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

# THE MEASURED ASSIGNMENT: role -> {"route", "model", "score"} written by automation/measure.py,
# which scores each candidate on the role's OWN contract (build_shotlist's plan, the caption
# judge's planted defect, the vision gate's labelled frames). Read at call time, so a fresh
# measurement lands on the next call with no restart. Absent or unreadable -> the plain order,
# which is what every run did before this existed.
_PICKS_MD = os.path.join(_DIR, "automation", "model_picks.json")
_picks_cache = (0.0, {})


def _picks() -> dict:
    """(mtime, picks) — re-read only when the file changes, so a run costs one stat per call."""
    global _picks_cache
    try:
        m = os.stat(_PICKS_MD).st_mtime
    except OSError:
        return {}
    if m != _picks_cache[0]:
        try:
            with open(_PICKS_MD) as f:
                _picks_cache = (m, json.load(f))
        except (OSError, ValueError):
            return _picks_cache[1]
    return _picks_cache[1]


# measurement-only pin. measure.py sets it to score ONE candidate at a time (no failover: a
# candidate that falls back is not the candidate being measured). Never set in a normal run.
_PIN = None

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


_STORY_MD = os.path.join(_DIR, "STORY.md")


def _story() -> str:
    """The house rules for WRITING (STORY.md), read at CALL time like the wavelength.

    Separate from WAVELENGTH.md on purpose: that file is the production contract (medium, captions,
    score, the hard nevers) and is pasted into every agent including the caption judge. These rules
    are only about the writing — the hook, the shape of the beats, the dialogue, the moral — so they
    go only to the agents that write words. Same bargain as the wavelength: a rule lives in a file
    rather than inside these prompt strings so a model swap cannot lose it, and an edit lands on the
    very next run. "" when absent, so the pipeline still runs without it."""
    try:
        with open(_STORY_MD) as f:
            return f.read().strip()
    except OSError:
        return ""


_brief_cache = None


def _brief() -> str:
    """The brain's own craft lessons, put in FRONT of every agent — the pre-generation trigger.

    Before this, the brain was consulted only AFTER the fact: `absorb()` audited the finished
    reel and `qc.run()` gated it, so a lesson learned from one reel (a card on a face, text the
    generator baked in, dead air between lines) could not shape the NEXT one's prompts — it could
    only fail it at the gate. This reads the same `craft_lessons` the gate enforces and hands them
    to the writer, shot-lister and art-director as rules to obey WHILE writing.

    Process rules (commit hygiene, db paths, monitoring) are filtered out — see
    State.film_lessons. Fail-open: no brain / no db -> "", and the pipeline runs unchanged."""
    global _brief_cache
    if _brief_cache is not None:
        return _brief_cache
    try:
        from state import State
        st = State()
        try:
            rows = st.film_lessons()
        finally:
            st.close()
        if rows:
            _brief_cache = ("RULES THIS PROJECT HAS ALREADY LEARNED — every one of these was a "
                            "real defect in a shipped reel. Obey them while you write:\n"
                            + "\n".join(f"- {r['title']}" for r in rows))
        else:
            _brief_cache = ""
    except Exception:  # noqa: BLE001 — a missing brain must never cost a reel
        _brief_cache = ""
    return _brief_cache


def _system(role: str, writing: bool = False) -> str:
    """A role prompt with the house wavelength AND the brain's learned craft rules in front of
    it, so no agent writes without them — and so a lesson the brain has just learned lands on
    the very next run.

    `writing=True` also puts STORY.md in front of it, for every agent that puts words on the reel
    (the script, the shot list, the caption cards) and for the script critic, whose rubric STORY.md
    IS. The caption judge leaves it off: its prompt is already long and its job is one line at a
    time, and a judge primed with the whole craft document drifts into rewriting what it audits."""
    parts = [_wavelength()]
    if writing:
        parts.append(_story())
    parts += [_brief(), role]
    return "\n\n---\n\n".join(p for p in parts if p)


# A free router answers a 429 "Too Many Requests" most days. Falling through to the next gateway
# on the first one meant a single rate-limit could cost the whole run — the route was fine, it
# just needed a second. Retries are per gateway: waiting out a busy free tier beats abandoning it.
_TRIES = int(ENV.get("LLM_RETRIES", "3"))
_TRANSIENT = ("429", "Too Many Requests", "500", "502", "503", "504", "timed out",
              "Connection reset", "Remote end closed", "empty content")


def _retryable(err) -> bool:
    """A busy or broken-looking server is worth another go; a 401/403/404 never is."""
    return any(k in str(err) for k in _TRANSIENT)


def chat(messages, max_tokens=4000, temperature=0.8, prefer=None, role=None):
    """Try each gateway in order; return (text, "gateway:model") from the first
    that returns non-empty content. Raises if all fail.

    `prefer` moves ONE gateway to the front of the queue without removing the others. The caption
    proofread uses it to ask a route the writer is unlikely to have used: the authoring call takes
    whichever gateway answers first, so the judge takes the other end of LLM_ORDER — a model that
    did not write the line. A preferred gateway that is down costs nothing; the rest still runs.

    `role` asks for the assignment MEASURED for that agent (model_picks.json, written by
    automation/measure.py): the winning route leads and its model is pinned. Measured, not
    assumed — each role is scored on its own contract, and a role with no measurement keeps the
    plain order. The rest of the order still runs behind the pick, so a subordinate takes over
    without an interruption whenever the winner is walled or slow.

    A route the LAST call found walled is skipped, not re-discovered (routerhealth): a 429 costs
    three retries and ~4.5s of sleep per call, and the gate makes dozens of calls per run. If
    every route is cooling they are all tried anyway — a memory of failure must never be the
    reason a run fails."""
    pin_model = {}
    order = list(_ORDER)
    pick = (_picks().get(role) or {}) if role else {}
    if pick.get("route"):
        pin_model[pick["route"]] = pick.get("model")
        order = [pick["route"]] + [n for n in order if n != pick["route"]]
    if prefer:
        order = [prefer] + [n for n in order if n != prefer]
    if _PIN:                                 # measurement: exactly this route, no failover —
        order = [_PIN[0]]                    # a candidate that fell back is not the candidate
        pin_model[_PIN[0]] = _PIN[1]
    cold = [n for n in order if routerhealth.cooling(n)[0]]
    if len(cold) == len(order) and not _PIN:
        routerhealth.clear()             # nothing left to skip: last resort beats giving up
    errors = []
    for name in order:
        url, key, model, timeout = _GATEWAYS.get(name, (None, None, None, 30))
        if not url or not key:
            continue
        model = pin_model.get(name) or model
        until, why = routerhealth.cooling(name)
        if until:
            errors.append(f"{name}[cooling {int(until - time.time())}s: {why}]")
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
                    routerhealth.note_success(name)
                    return content, f"{name}:{d.get('model', model)}"
                raise ValueError("empty content")
            except Exception as e:  # noqa: BLE001 - any failure -> retry, then next gateway
                errors.append(f"{name}[{attempt + 1}]: {e}")
                if attempt + 1 < _TRIES and _retryable(e):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        routerhealth.note_failure(name, errors[-1] if errors else "failed")
    raise RuntimeError("all LLM gateways failed: " + " | ".join(errors)
                       + (f" | cooling: {routerhealth.walled()}" if routerhealth.walled() else ""))


# The script call used to ask for 2000 tokens, and that budget CUT the newest script mid-table:
# projects/dadi-ki-bucket-list/script.md ends on a bare "| 8 | **" with its NGO-card row missing.
# Nothing noticed — agent 2 read the truncated document, the folder kept it, the reel was built
# from it. Devanagari is token-hungry (about a token per character on these routers), so a Hindi
# script with a beats table needs far more room than an English one of the same length.
#
# 8000 rather than 4000: the CRITIC has to re-emit the whole corrected script inside its JSON, so
# its answer is the script plus the findings, escaped. Measured on dadi-ki-bucket-list (a 3.5KB
# script): 4000 came back as an unterminated JSON object, 8000 came back whole and parsed.
SCRIPT_TOKENS = int(ENV.get("SCRIPT_TOKENS", "8000"))

# How many times the critic may send the script back: one rewrite, then one check of that rewrite.
# Never unbounded — a free tier that always finds something to say must not turn one kickoff into
# twenty calls. 0 switches the critique off and restores the old single-draft behaviour.
SCRIPT_ROUNDS = max(0, int(ENV.get("SCRIPT_ROUNDS", "2")))

# The critique of the script that was actually written, for the caller to record beside it. A module
# global rather than a second return value on purpose: write_script's contract (idea -> script text)
# is stubbed by measure.py and qc.py's selfchecks, and widening it would break all of them.
LAST_CRITIQUE = {}

_SCRIPT_SECTIONS = ("LOGLINE", "VISUAL STYLE", "BEATS", "MORAL")


def _script_complete(text: str) -> list:
    """What is missing from a script — [] when it is whole.

    Its whole job is truncation. A model that runs out of token budget mid-table returns a document
    that looks like a script to everything downstream: agent 2 builds a shot list from it, the folder
    keeps it as the reel's record, and the rows that never arrived are simply never missed. A last
    line that stops inside a table row, or carries an odd number of bold markers, is what a cut-off
    answer looks like."""
    t = (text or "").strip()
    if not t:
        return ["the answer was empty"]
    up = t.upper()
    bad = [f"no {s.title()} section" for s in _SCRIPT_SECTIONS if s not in up]
    last = [ln.strip() for ln in t.splitlines() if ln.strip()][-1]
    if last.startswith("|") and not last.endswith("|"):
        bad.append(f"it stops inside a table row: {last[:48]!r}")
    elif last.count("**") % 2:
        bad.append(f"it stops inside bold text: {last[:48]!r}")
    return bad


_SCRIPT_WRITER = (
    "You are an award-winning short-film director and screenwriter, writing for an Indian family "
    "scrolling on a phone. You turn a one-line idea into a script nobody can scroll past. You write "
    "what the audience SEES and HEARS, never vague abstractions. STORY.md is your craft standard and "
    "you obey every rule in it — the hook, the shape of the beats, the spoken register, the word "
    "budget, the earned moral. You have never met a reel you could not make specific."
)

# The critic's rubric IS STORY.md, so this prompt only says how to judge and what to return. It is
# built on the shape that already works here — agents.judge_captions: an independent reader that did
# not write the thing, must quote what it objects to, and must return the fix rather than advice.
_SCRIPT_CRITIC = (
    "You are a story editor for a short vertical film. You did NOT write this script, and you are "
    "not here to be encouraging — you are the last reader before a single frame is generated, so "
    "anything you let pass gets filmed.\n"
    "Judge it against STORY.md, rule by rule, and TAG every problem with the rule it breaks, so a "
    "weakness that recurs across reels can be counted: [hook] [shape] [concrete] [dialogue] "
    "[budget] [moral] [sameness] [length].\n"
    "For each problem:\n"
    "- QUOTE the exact line you object to. A finding that does not quote the script is not a "
    "finding.\n"
    "- Say what the rule wanted instead, in one sentence.\n"
    "- Look for the whole CLASS, not the instance: if one beat is a listicle item, check every other "
    "beat for the same fault; if one line is written Hindi, read all of them.\n"
    "The three findings that matter most, in this order: a hook that is a preface or a contents "
    "page; beats that could be swapped with no loss because nothing escalates and there is no turn; "
    "a moral that is a platitude, a question, or unsupported by the story.\n"
    "COUNT the words of every spoken line against its seconds — a line of the wrong length cannot be "
    "performed, and no edit fixes it after the fact.\n"
    "Then REWRITE the script yourself and return the WHOLE corrected document in the same shape "
    "(MEDIUM, LOGLINE, VISUAL STYLE, BEATS table, MORAL, CTA), at the length the idea asked for. "
    "Every problem you name must be VISIBLY fixed in what you return, and every line you changed "
    "must appear in problems. Rewrite in the script's own language and register — the dialogue stays "
    "Hindi as a character would speak it, never literary.\n"
    "If the script already meets every rule, return it unchanged and an empty problems list."
)


def _shipped_shapes(limit: int = 8) -> list:
    """The structure/motif/moral of the reels already shipped — the 'never the same reel twice' rule
    needs the writer and the critic to SEE what has been done. `other_identities("")` matches no reel
    by name, so it returns every one of them. Fail-open: no brain, no shapes, and the rule is simply
    unenforced for that run."""
    try:
        from state import State
        st = State()
        try:
            rows = st.other_identities("")
        finally:
            st.close()
        out = []
        for r in rows:
            try:
                fp = json.loads(r["fp"])
            except (TypeError, ValueError):
                continue
            keep = {k: fp.get(k) for k in ("structure", "motif", "moral", "opener") if fp.get(k)}
            if keep:
                out.append({"reel": r["reel"], **keep})
        return out[-limit:]
    except Exception:  # noqa: BLE001 — a missing brain must never cost a script
        return []


# a problem's leading tag, e.g. "[shape] the four beats can be swapped"
_TAG = re.compile(r"\[([a-z][a-z-]{2,19})\]")

# A finding as it appears inside the critic's JSON — "[hook] the exact line — what the rule
# wanted". Used only to salvage findings out of an answer that was cut off before its closing brace.
_SALVAGE = re.compile(r'"(\[[a-z][a-z-]{2,19}\][^"]{12,})"')


def _salvage_problems(text: str) -> list:
    """The findings out of an unparseable critic answer. A truncated JSON object still contains
    every problem it got to, as complete quoted strings; they are read here so a cut-off answer
    still teaches the brain something instead of being thrown away as an outage."""
    return [m.group(1).strip() for m in _SALVAGE.finditer(text or "")]


def _file_script_lessons(problems: list):
    """A finding the critic keeps making becomes a candidate rule the brain proposes — and the owner
    still vets it: nothing filed here is enforced, it waits in `candidate_lessons` for `brain.py
    promote`.

    It goes through the existing propose path for one reason: propose_lesson upserts by slug and
    increments `hits`, so a fault seen on four reels reaches the owner with hits=4 while a one-off
    reaches them with hits=1 — recurrence is counted for free instead of by a counter kept here. The
    title is per TAG and therefore stable; the newest evidence rides in `why`."""
    try:
        from state import State
        st = State()
    except Exception:  # noqa: BLE001 — no brain, no lesson, no cost
        return
    try:
        for p in problems:
            m = _TAG.search(p or "")
            if not m:
                continue
            tag = m.group(1)
            st.propose_lesson(
                f"script-{tag}",
                f"The script's {tag} keeps failing the critic — write to STORY.md",
                why=(p or "").strip()[:400],
                evidence=(p or "").strip()[:400])
    except Exception:  # noqa: BLE001
        pass
    finally:
        st.close()


def critique_script(script: str, idea: str = "", priors=None) -> dict:
    """The script judge — the one review this pipeline never had.

    Every other part of a reel is measured before it ships: the pace, the captions, the faces, the
    audio floor. The script — the thing that decides whether anyone watches — was written once and
    read by nothing at all. This is that reader: it gets STORY.md as its rubric, the idea the script
    must serve and the shapes already shipped, and it returns problems plus the rewritten script.

    It asks the OTHER gateway (`prefer`, the same trick the caption judge uses): the writer takes
    whichever route answers first, so the critic asks the one that probably did not write it — a
    model marking its own homework is not a review. `role="judge"` reuses the route already MEASURED
    for judging rather than inventing an unmeasured role. Raises if no gateway answers; the caller
    decides what an outage means, so a check that could not run is neither a pass nor a failure."""
    shown = []
    for p in priors or []:
        p = p or {}
        bits = [f"{k}: {str(p[k])[:100]}" for k in ("structure", "motif", "moral") if p.get(k)]
        if bits:
            shown.append(f"- {p.get('reel', 'a reel')} — " + " | ".join(bits))
    seen = ("\nALREADY SHIPPED — their shapes, motifs and morals. Do not write one of these again:\n"
            + "\n".join(shown)) if shown else ""
    user = (
        f"THE IDEA THIS REEL MUST SERVE:\n{idea}\n\n"
        f"THE SCRIPT AS WRITTEN:\n{script}{seen}\n\n"
        # The contract goes LAST and forbids reasoning, for the reason the caption judge learned the
        # hard way: these routes are reasoning models, and one spent its whole budget thinking and
        # never emitted the JSON, so the answer read as a gateway outage.
        "Answer with JSON ONLY — no reasoning, no commentary, no markdown fences:\n"
        '{"problems": ["[tag] the exact line — what the rule wanted instead", "...", '
        '"[tag] ..."], '
        '"script": "the WHOLE corrected script, in the same document shape"}')
    text, used = chat(
        [{"role": "system", "content": _system(_SCRIPT_CRITIC, writing=True)},
         {"role": "user", "content": user}],
        max_tokens=SCRIPT_TOKENS, temperature=0.4,
        prefer=_ORDER[-1] if len(_ORDER) > 1 else None,
        role="judge",
    )
    try:
        out = _extract_json(text)
    except ValueError:
        # A cut-off answer is the normal failure of this call, not an exotic one: the critic has to
        # re-emit the whole script inside the JSON. Its findings, though, are complete strings long
        # before the end — so salvage them, keep the draft, and let the caller record what was
        # found. An unparseable answer is not an outage, and the findings are worth more than the
        # rewrite we could not read.
        out = {"problems": _salvage_problems(text), "script": ""}
    problems = [str(p).strip() for p in (out.get("problems") or []) if str(p).strip()]
    fixed = out.get("script") or out.get("rewritten_script") or ""
    return {"problems": problems, "script": fixed if isinstance(fixed, str) else "", "by": used}


def _finish_script(text: str, missing: list, idea: str) -> str:
    """One continuation call for an answer that ran out of room, and only that.

    The continuation is accepted only if it actually reduces what is missing, so a model that answers
    with a fresh preamble instead of a continuation leaves the original untouched."""
    try:
        more, _used = chat(
            [{"role": "system", "content": _system(_SCRIPT_WRITER, writing=True)},
             {"role": "user", "content":
              f"IDEA: {idea}\n\nYou were writing this script and your answer was cut off. What is "
              f"missing: {'; '.join(missing)}.\n\nThe end of what you wrote:\n…{text[-1200:]}\n\n"
              "Continue from exactly there and finish the document — the rest of the BEATS table, "
              "the MORAL line and the CTA. Return ONLY the continuation: no preamble, no repeated "
              "sections, no closing remarks."}],
            max_tokens=SCRIPT_TOKENS, temperature=0.6, role="script")
    except Exception:  # noqa: BLE001 — a draft is better than nothing
        return text
    joined = text.rstrip() + "\n" + more.strip()
    return joined if len(_script_complete(joined)) < len(missing) else text


def _draft_script(idea: str, seconds: int = None) -> tuple:
    """The writer's ONE call, plus the continuation call when its answer ran out of room. Split out
    of write_script so the draft can be scored on its own (measure.py ranks candidates on the draft,
    not on the rewrite, because the rewrite is partly the critic's work).

    `seconds` is the length the caller wants, and None means "whatever the IDEA asks for" — the old
    default of 15 was silently pasted into the prompt for a 35-second brief, so the writer was told
    to make a 15-second film while the idea asked for half a minute and the two fought each other.
    Returns (text, "route:model")."""
    system = _system(_SCRIPT_WRITER, writing=True)
    length = (f"~{seconds}-second" if seconds else
              "cinematic video of the length the IDEA asks for — about 30 seconds if it names none")
    user = (
        f"IDEA: {idea}\n\n"
        f"Write a script for a {length} video. Include:\n"
        "- MEDIUM: this is a 3-D ANIMATED film — a stylized animated-feature look, never photoreal, "
        "never live action. Write every beat as it would look animated.\n"
        "- LOGLINE: one sentence — who wants what, and what it costs them.\n"
        "- VISUAL STYLE: palette, 3-D animation look, mood (one paragraph).\n"
        "- BEATS: a numbered table — the visual action, the exact Hindi line a character speaks on "
        "camera (or *silent*), and the seconds that beat runs. Count each line's words against the "
        "STORY.md budget before you write it down.\n"
        "- MORAL: the closing card line, written out.\n"
        "- CTA: the closing line and the brand on the last frame, kept separate from the moral.\n"
        "- CLOSING VOICEOVER (the owner's standing rule, 2026-10-22): the reel's LAST beat must "
        "carry a line SPOKEN IN THE SCENE — a character says it to camera, in Hindi, and it is "
        "engaging, emotional and relatable: the thing the viewer carries away, said out loud, not "
        "just read off a card. It is spoken by someone IN the scene (never a narrator, never a "
        "recorded voice laid over the film) and it is the last thing said. Write it as its own "
        "beat, in the beat table, with the seconds it runs.\n"
        "Write the WHOLE document — an answer that stops mid-table is treated as broken. "
        "Keep it tight, concrete, and directable."
    )
    text, used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=SCRIPT_TOKENS, temperature=0.9, role="script",
    )
    cut = _script_complete(text)
    if cut:
        print(f"  ! the script came back incomplete ({'; '.join(cut)}) — asking for the rest")
        text = _finish_script(text, cut, idea)
    return text, used


def write_script(idea: str, seconds: int = None) -> str:
    """Agent 1: idea -> a script that has been judged and rewritten before anything is filmed.

    The critique is the point of this function. The script used to be written once and read by
    nothing; now it is written, sent to a model that did not write it, and rewritten to its findings
    — twice at most — before a single video credit is spent. The whole exchange costs a few seconds
    on a free tier against the ten to thirty minutes and the credits a render costs.

    The draft is always returned, whatever happens: an unjudged script beats no reel at all when the
    critic's gateway is down. What happened is left in LAST_CRITIQUE, and the pipeline writes it
    beside the script so the record survives the run."""
    global LAST_CRITIQUE
    text, used = _draft_script(idea, seconds)
    cut = _script_complete(text)      # what was still missing after the one continuation call

    problems, rounds, by, outage = [], 0, "", ""
    for _ in range(SCRIPT_ROUNDS):
        try:
            out = critique_script(text, idea, _shipped_shapes())
        except Exception as e:  # noqa: BLE001 — an unjudged script is still a script
            outage = str(e)[:200]
            break
        rounds += 1
        by = out.get("by") or by
        problems += out["problems"]
        if not out["problems"]:
            break
        fixed = out["script"]
        # only adopt a rewrite that is whole: a critic that ran out of room mid-sentence and handed
        # back half a document must not leave us with less than we started with
        if fixed.strip() and not _script_complete(fixed):
            text = fixed
        else:
            break
    LAST_CRITIQUE = {"problems": problems, "rounds": rounds, "by": by, "writer": used,
                     "could_not_run": outage, "incomplete_on_arrival": cut}
    if problems:
        print(f"  script critic: {len(problems)} finding(s) over {rounds} pass(es) — rewritten")
    elif rounds:
        print(f"  script critic: clean on the first read ({by})")
    if outage:
        print(f"  ! the script critic could NOT run ({outage[:80]}) — the script ships as written")
    _file_script_lessons(problems)
    return text


_SCHEMA = """{
  "title": "short title",
  "slug": "a short lowercase ASCII folder name for this reel — letters, digits and dashes only, e.g. 'the-tailors-thread'. A Hindi title MUST be transliterated here; this names the reel's folder on disk, so it must not be empty or a placeholder.",
  "world": "the LOCKED universal anchor: setting/place, time of day, weather, season, overall lighting & color atmosphere, and the fixed description of EVERY recurring person — the lead AND every other person who appears in more than one clip (who/what, exact clothes/colors/features, and what each one does NOT have). This never changes across clips. It MUST also state that the room and every surface in it are clean of writing — no text, signage, papers with words or lettering.",
  "style": "the 3-D animation anchor from WAVELENGTH.md §1 — the phrase every clip prompt opens AND closes with (rounded shapes, soft global illumination, cinematic lighting, shallow depth of field)",
  "music_mood": "search words for the background score this reel needs, in plain English — e.g. 'mournful solo bansuri evening raga' or 'playful indian folk dholak festive'. Used to search for a track when nothing in the library fits.",
  "music_want": "one of exactly: bright | playful | warm | neutral | tender | mournful | sombre — where this reel's score sits on the brightness scale. A comic reel is bright; a reel about loss is mournful.",
  "aspect": "9:16",
  "clips": [
    {"idx": 0, "prompt": "a COMPLETE text-to-video prompt for ONE ~5s shot: the action + camera move in THIS shot. It will be sent to a frontier video site as-is, with 'world' prepended and 'style' appended automatically. Name ONE gentle camera move (slow push-in / pull-back / pan / drift). Never ask the shot to show text or a caption card, or to move the camera INTO a frame or object.", "dialogue": "the Hindi line a character SPEAKS ON CAMERA in this shot, or an empty string for a silent beat. This is written into the generation prompt so the clip's own audio carries it — there is no narration track added later."}
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
        "- MEDIUM: this reel is 3-D ANIMATION — 'style' MUST describe a stylized animated-feature "
        "look (rounded shapes, soft global illumination) and must contain the words '3-D "
        "animation'. Never photoreal, never live action, never documentary or real footage. If "
        "'style' does not read as animation the build REPLACES it with the house anchor, so write "
        "it as animation from the start.\n"
        "- Write 'world' FIRST and make it rich and specific — it is prepended to "
        "every clip prompt, so no clip can drift or hallucinate a new place/character.\n"
        "- 3 to 6 clips. Each 'prompt' is a COMPLETE, self-contained ~"
        f"{clip_seconds}s shot description (action + camera move) that reads well on "
        "its own. Do NOT restate 'world' inside it — it is added automatically.\n"
        "- COVER EVERY BEAT. The script's beats are the film: fold two beats into one shot when "
        "they happen in one place with the same people, but never drop one. The script's LAST "
        "beat is the landing the whole reel was written for — it is ALWAYS its own clip, and the "
        "reel ENDS on it. A reel that stops on the problem and never shows the resolution has "
        "lost the only thing the viewer was waiting for (a grandfather's reel ended on his "
        "reproach, with the family's answer — the script's own final beat — cut away).\n"
        "- The subject's description must ALSO name what they do NOT have — end it with "
        "the negatives explicitly (e.g. 'no glasses, no hat, no jewellery') unless an "
        "accessory is deliberate and then state it in 'world'. A lock that lists only "
        "what IS there cannot stop what gets ADDED: the generator invented round glasses "
        "on one clip of one reel and on no other, and every prompt still dutifully named "
        "the same man.\n"
        "- EVERY PERSON WHO APPEARS IN MORE THAN ONE CLIP must be described in 'world' with "
        "that same detail — not only the lead. The generator has no memory between clips, so "
        "anyone left out of 'world' is played by a DIFFERENT person in each clip that shows "
        "them (this is how one reel shipped three different women as the same grandmother). "
        "A family, a crowd or a passer-by who appears in ONE clip only is named in that clip "
        "prompt and kept generic; anyone who returns is locked in 'world' first.\n"
        "- Keep the recurring subject identical shot to shot. Use gentle, continuous "
        "camera moves (slow push-in, drift, pan) so consecutive clips cut together smoothly.\n"
        "- CAMERA MOVES: ONE move per clip, and it must be gentle and continuous. NEVER write "
        "a camera that flies, glides, dives or travels INTO a frame, a photo, a screen or an "
        "object — the generator obeys it literally and the shot becomes a lurching zoom nobody "
        "asked for (the owner cut exactly that move out of a reel). A memory shown INSIDE a wall "
        "frame is reached by cutting to it, or by a slow pan or drift ACROSS it — never by the "
        "camera entering it. Slow push-in, slow pull-back, pan, drift: those four, nothing else.\n"
        "- NO TEXT, NO CAPTIONS, NO SUBTITLES — with ONE exception: a phone or tablet screen may "
        "show the message the story is about (the chat bubble being read or typed), because a reel "
        "whose plot IS a message has to be able to show it. Nothing else written, printed, drawn, "
        "embroidered, engraved or shown on any other surface in the shot: no words on walls, "
        "signboards, papers, books, TV screens, labels or clothing; no subtitle bars, no title or "
        "caption cards, no lettering of any kind, in any script. State this in every clip prompt in "
        "your own words, positively — 'a clean frame with no writing anywhere except the phone "
        "screen' — because the film must carry no text of its own: every caption and card is added "
        "to the finished cut afterwards. A prompt that asks the generator for a caption, a title, "
        "a heading, or text on anything but a phone gets gibberish Devanagari baked into the frame, "
        "and the real card then has to sit over it and read as fake (the owner caught exactly this "
        "in a closing shot).\n"
        "- 'music_mood' + 'music_want' describe the SCORE the reel needs, and must agree "
        "with its feeling: a solemn raga under a comic beat is the wrong film. Write the "
        "mood as words someone would actually search for.\n"
        f"- aspect '{aspect}' (vertical/portrait for reels).\n"
        "- Return JSON ONLY."
    )
    # A parse that fails is retried once, not fatal. The model that wrote a literal newline inside
    # a string took the whole kickoff down as a traceback — a lost run over a control character,
    # from the cheapest call in the pipeline (no render is bought here either way).
    shot = None
    for attempt in range(2):
        if attempt:
            user += ("\n\nYOUR LAST ANSWER WAS NOT VALID JSON and was rejected. Return the SAME "
                     "plan as a single JSON object, with every string on ONE line.")
        text, used = chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=4000, temperature=0.7, role="shotlist",
        )
        try:
            shot = _extract_json(text)
            break
        except Exception as e:  # noqa: BLE001 — a bad answer is retried, not fatal
            err = str(e)[:160]
    if shot is None:
        raise RuntimeError(f"the shot list came back unreadable twice: {err}")
    # Decide the medium at the START, not after a render: a plan whose style does not read as
    # animation gets the house 3-D anchor here, so the shot list itself is honest and the script,
    # the shot list and the clip prompts all agree (owner rule 2026-10-10). clip_prompts enforces
    # it again on the way out; this is the earliest catch.
    if isinstance(shot, dict) and not _is_animated(shot.get("style") or ""):
        shot["style"] = HOUSE_STYLE
    return shot


PROMPT_CHARS = 4000          # keep a typed prompt inside every site's input comfortably

# A caption card renders into a 560px box at 41px (caption.FONTS/caption.render); past ~34 a
# "line" is no longer one glance. CARD_CHARS is what the prompt ASKS for; CARD_HARD is what the
# contract REJECTS. They are two numbers on purpose: no model counts Devanagari characters
# reliably, and failing the whole art direction (identity AND every card) over +2 chars meant a
# repair loop the model could not win. 34-44 wraps to a second visual line and still reads on a
# phone; past 44 it is a paragraph the card cannot hold.
CARD_CHARS = 34
CARD_HARD = 44
# How many times agent 3 may rewrite its own cards after the contract rejects them. A rejection
# with the reason fed back is cheap; a reel queued with no art direction is not.
PRESENTATION_TRIES = 3
CARD_KINDS = ("hook", "moral", "cta")
_DEVA = re.compile(r"[ऀ-ॿ]")

# WAVELENGTH §8 names the foundation, and its domain is a FACT about the brand — not a creative
# choice for the model to make. A model left to invent one wrote "standardsunrise.org" onto a
# shipped card. So the domain is stated in the wavelength AND written by code here, and the
# self-check asserts the two agree (drift between the prose and the constant is the failure mode).
BRAND_URL = "standardsunrisefoundation.org"
_DOMAIN = re.compile(r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9-]+\.(?:org|com|in|net|co|io)\S*")


def _fix_url(text: str) -> str:
    """Any web address on a card is the foundation's — or it is wrong."""
    return _DOMAIN.sub(BRAND_URL, text)


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


# A character tag is an ALL-CAPS word — the shot-lister is told to write the recurring subject
# that way, and it is the only handle code has on WHO must not change.
_CHAR_TAG = re.compile(r"\b[A-Z]{3,}\b")
_CHAR_CUE = re.compile(r"\b(?:subject|character|protagonist|cast|person|figure)s?\b", re.I)
_SENT = re.compile(r"(?<=[.!?।])\s+")


def _char_clauses(world: str) -> list:
    """The sentences of `world` that describe a CHARACTER — the ones naming an ALL-CAPS tag, or
    otherwise saying "subject"/"character". A plan that writes neither (the model does not
    always) still deserves a lock, and the sentence to fall back to is the LAST one, not the
    first: `world` is specified as setting/place/time/light FIRST and "the recurring subject's
    fixed description" LAST, so the opening sentence is the room.

    Quoting the opening sentence (what this did) told the generator "the characters are exactly
    these, verbatim: A cozy living-room in a small Indian town..." — the cast description never
    reached the prompt at all, which is the same hole that let dadi ship three different women
    playing one character. `dadaji-ka-kahani` (whose world names no tag) locked the ROOM and
    left the grandfather to the model's memory.

    ponytail: the quoted description is capped (240 chars, 4 clauses) so a rambling `world`
    cannot make the lock longer than the prompt it is appended to. Raise the cap if a measured
    cast description is being cut mid-way and the faces drift."""
    sents = [s.strip() for s in _SENT.split((world or "").strip()) if s.strip()]
    named = [s for s in sents if _CHAR_TAG.search(s)]
    cued = [s for s in sents if _CHAR_CUE.search(s)]
    out = []
    for s in (named or cued or sents[-1:])[:4]:
        out.append(s if len(s) <= 240 else s[:240].rsplit(" ", 1)[0])
    return out


# ---- what makes a clip a FLASHBACK -----------------------------------------------------------
# The plan already says it in words ("Flashback: Young Dadaji dashes through a flood", "Back to
# present: …"). ONE list, read by the two places that must agree about it: _clip_lock below (so a
# flashback clip is not ordered to hold an age the story changes) and the QC character check (so a
# younger face is not read as a drifted one). A jump the plan never names is invisible here; the
# by-eye pass is what catches that one.
FLASHBACK_WORDS = ("flashback", "flash-back", "flash back", "time jump", "years earlier",
                   "years ago", "in the past", "back then", "memory", "memories", "younger",
                   "young version", "as a child", "childhood")


def declares_flashback(text: str) -> bool:
    """Does this clip's own text say it happens in an earlier time? ("Back to present" does not —
    it is the RETURN from the jump, and marking it would move the exclusion onto the wrong clip.)"""
    low = (text or "").lower()
    return any(w in low for w in FLASHBACK_WORDS)


def _clip_lock(shot: dict, clip: dict, base: str = "") -> str:
    """The identity lock for ONE clip — the reel's single lock, in its flashback form when this
    clip's own text declares an earlier time.

    A flashback clip cannot carry the standard lock. That lock says "identical face, AGE … from the
    first clip to the last, and no … garment … removed", in the same prompt whose action line opens
    "Flashback: Young Dadaji …" and whose cast block calls him an elderly man — so the generator
    is handed two opposite orders about the one thing the shot is about, and picks one (the same
    class of bug NO_TEXT exists for; dadaji-ki-yadein, 2026-10-19). The flashback form keeps every
    guarantee that still holds — same face structure, features, hair, skin tone and build, nothing
    added or redesigned — and drops only what the story itself changes: age, and the clothes and
    light of that earlier day."""
    base = base or _char_lock(shot)
    if not declares_flashback((clip.get("prompt") or "") + " " + (clip.get("visual") or "")):
        return base
    return ("FLASHBACK — THE SAME PERSON AS IN EVERY OTHER CLIP, AT A YOUNGER AGE. This clip is an "
            "earlier time in the story, so this character is YOUNGER here and wears what he wore "
            "then: where the cast description above reads as an elderly man, read it as the SAME "
            "man, young. Keep the same face structure, features, hair and skin tone and the same "
            "build, so he is recognisable as that person; nothing else added, removed or "
            "redesigned, and no accessory or jewellery he does not have.")


def _char_lock(shot: dict) -> str:
    """ONE sentence per reel, appended VERBATIM — same words, same place — to every clip prompt.

    Byte-identical is the point, not a nicety. The old lock was assembled from the character
    TAGS alone ("same face, same hair, same glasses"), which tells a generator nothing it can
    hold a face against, and a plan with no ALL-CAPS tag got no lock at all — that is how dadi
    shipped three different women in one reel while every prompt dutifully named the same
    character. Quoting the character's OWN description back, unchanged, on every clip, is what
    keeps the person the same from the first scene to the last: the identity is re-read at
    generation time instead of the model being asked to remember it.
    """
    clauses = _char_clauses((shot.get("world") or "").strip())
    if not clauses:
        return ""
    who = " ".join(clauses)
    return ("THE SAME PEOPLE IN EVERY CLIP — identical face, age, hair, build, clothing and "
            "colouring from the first clip to the last, and no accessory, garment, jewellery, "
            "make-up or feature added, removed or redesigned along the way. The characters are "
            f"exactly these, verbatim: {who}")


# Attached to EVERY clip prompt by code, not by a model's good behaviour. The generator bakes
# gibberish lettering into a frame the moment a prompt even hints at text (a "caption card", a
# sign), and the real card then reads as a fake pasted over nonsense — the owner caught it in a
# closing shot. Asking the LLM to remember is not a guarantee; this is.
#
# ONE exception, and it is the owner's (2026-10-16): on-screen text is allowed ON A PHONE SCREEN,
# and nowhere else. A reel whose whole plot is a message nobody answers has to be able to show the
# message — this rule as written forbade the one image the story is made of, in the same prompt
# that asked for it, and the generator was left to pick a side. The ban stays absolute everywhere
# else because that is the case the rule was written for.
NO_TEXT = ("No text of any kind anywhere in the frame EXCEPT on a phone or tablet screen, which "
           "may show the message the story is about: no other words, letters, captions, "
           "subtitles, title cards, signage, labels or writing on walls, papers, clothing or any "
           "other surface — in any script. All caption cards are added later.")

# The shot-lister is TOLD no text, but it writes the sentence it was told not to: asked to
# visualize a notebook of memories, it authored "a glowing \"एक\" appears on the page" — text
# baked into the frame, which is exactly what makes the generator render gibberish Devanagari.
# Appending NO_TEXT is not prevention when the authored prompt ASKS for the text (the generator
# gets two opposite orders). So the request is stripped by CODE here, not by the model's good
# behaviour — the same rule the NO_TEXT appended to every prompt follows. A prompt that avoids
# text entirely (the normal case) is returned untouched.
# Only a REQUEST counts, and it must be told apart from the two things that look like it: a spoken
# line in quotes, and a protective negation ("no writing anywhere"). Three guards, in order:
#   - a quoted token that is DISPLAYED (quote then a display verb or a text-bearing object) is
#     on-screen text. Dialogue is the reverse order — a speech verb then the quote
#     (`says, with clear lip-sync: "…"`) — so a spoken line can never match and is never touched.
#   - a word that only ever names a text device (spell/spelling, subtitle, title/caption card).
#   - but a device word inside a NEGATION ("no writing or signage anywhere") is the protective
#     sentence agent 2 was TOLD to write — kept, not stripped.
_QUOTED = re.compile(
    r'["“][^"”\n]{1,40}["”][^.]{0,30}?\b(?:appears?|glows?|shines?|flashes?|reads?|written|'
    r'sign|signboard|label|plate|board|banner|poster|notice|card)\b', re.I)
_DEVICE = re.compile(
    r'\b(?:spell(?:s|ing|ed)?|spelt|lettering|signage|subtitles?|title\s+cards?|caption\s+cards?|'
    r'the\s+words?)\b', re.I)
_NEG = re.compile(r'\b(?:no|not|nothing|without|never|free|clean|blank)\b', re.I)
# The owner's one exception (2026-10-16): a phone screen may carry the message the story is about,
# so a clause that asks for THAT is a legitimate shot and must survive. Checked before the other
# two guards, because those would otherwise strip the single image the reel exists to show.
_PHONE = re.compile(r'\b(?:phone|smartphone|mobile|tablet)\b', re.I)


def _asks_text(s: str) -> bool:
    if _PHONE.search(s):
        return False
    return bool(_QUOTED.search(s) or (_DEVICE.search(s) and not _NEG.search(s)))
# Clauses, not sentences: the text request often shares a sentence with the action it decorates
# ("she opens the notebook; a glowing \"एक\" appears"), so splitting at sentence ends alone would
# throw the action away with the request. Strong punctuation only — a comma would over-split. The
# lookbehind keeps each stop with the clause it ends, and danda is included for Hindi prompts.
_CLAUSE = re.compile(r'(?<=[.!?।;—–])\s+')
_LETTER = re.compile(r'\w')                       # \w is Unicode-aware: Devanagari counts


def _no_text(prompt: str) -> str:
    """Drop any clause of an authored shot that asks the generator to show text. Fail-open: a
    prompt with no text request comes back unchanged."""
    s = (prompt or "").strip()
    if not _asks_text(s):
        return s
    kept = [c for c in _CLAUSE.split(s)
            if c and not _asks_text(c) and _LETTER.search(c)]
    out = " ".join(kept).strip()
    return out if out else ("A clean frame carrying no writing of any kind — only the "
                            "scene's physical action and expression")


# The house medium, enforced by code. This house renders 3-D animation, always (WAVELENGTH.md §1;
# owner rule 2026-10-10 for every Standard Sunrise reel). A plan's own style wins when it IS
# animated — that is what gives each reel its distinct look — but a missing or non-animated style
# is REPLACED by this, the same guarantee NO_TEXT makes. A photoreal plan (or a generator that
# ignored the prompt) can no longer slip a real-looking frame past the pipeline.
HOUSE_STYLE = ("soft stylized 3-D animation in a high-end animated-feature look: rounded shapes, "
               "soft global illumination, cinematic lighting, shallow depth of field")

# Stated at the very START of the build so the MODEL knows the medium BEFORE it generates, not
# after (owner rule 2026-10-10: "make sure it is identified at the beginning of the project before
# the video is generated"). It reaches every layer — agent 1's script prompt, agent 2's shot list
# and its returned `style`, and the FIRST line of every clip prompt that actually goes to a site.
MEDIUM_REQUIREMENT = ("REQUIRED OUTPUT: 3-D ANIMATED VIDEO — a stylized animated-feature look in "
                      "every frame, never photoreal, never live action, never real footage, never 2-D.")

# A DISASTER shot leads with the CARTOON form instead. Veo photorealises flood, rain and storm
# whatever the prompt says — the generic 3-D lead did not hold the dhimi-chal flashback, which
# came back live action TWICE (2026-10-22, once even after a re-shoot with the rejection folded
# into the prompt). The lead is the one place the site reads FIRST, so for these shots it has to
# name the cartoon form outright.
CARTOON_LEAD = ("REQUIRED OUTPUT: PIXAR-STYLE CARTOON 3-D — chunky rounded cartoon "
                "proportions, stylized animated-feature character, a frame from an "
                "animated movie, NOT photoreal, NOT live action, NOT real footage. "
                "STYLE for every frame: ")

_DISASTER = re.compile(
    r"\b(?:flood|inundat|storm|cyclone|typhoon|hurricane|rain(?:ing)?|river|torrent|"
    r"blaze|fire|earthquake|rubble|wav(?:es|es)?|surge)\b", re.I)

# The sites that run the SAME engine — Veo 3.1 — and are the waterfall's two primaries
# (WAVELENGTH §5: Vids → Flow → the rest). They share one prompt pattern, so they share one
# shape below; a reel that fails over between them is not re-written.
VEO_SITES = ("flow", "googlevids", "veo")


def _fit(lead: str, body: str, tail: str, chars: int, sep: str, floor: str = "") -> str:
    """Trim to `chars` in the order that keeps the GUARANTEES.

    The tail is the guarantee set (look, no-text, anti-photoreal, the identity lock) and the
    lead carries the medium requirement, so neither is what gives: a shorter action still
    renders the same person in the same look, a lock trimmed off the end of a prompt does not.
    The SHOT shortens first, then the lead goes (the LOOK line restates the whole style anyway)
    — but `floor`, the irreducible part of the lead, stays. Dropping the whole lead used to drop
    MEDIUM_REQUIREMENT with it, which IS the 3-D rule: the longest clip in the plan, the one
    that most needed telling, was the one that could render photoreal.
    """
    seg = sep.join(x for x in (lead, body, tail) if x)
    if len(seg) <= chars:
        return seg
    keep = chars - len(lead) - len(tail) - 2 * len(sep) - 1
    if keep > 0:
        fitted = sep.join(x for x in (lead, body[:keep].rsplit(" ", 1)[0], tail) if x)
        if len(fitted) <= chars:
            return fitted
    seg = sep.join(x for x in (floor, body, tail) if x)
    if len(seg) <= chars:
        return seg
    keep = chars - len(floor) - len(tail) - 2 * len(sep) - 1
    if keep > 0:
        fitted = sep.join(x for x in (floor, body[:keep].rsplit(" ", 1)[0], tail) if x)
        if len(fitted) <= chars:
            return fitted
    return seg[:chars - 1].rsplit(" ", 1)[0] + "."


def clip_prompts(shot: dict, site: str = "") -> list:
    """Materialize the final per-clip prompts. This is what gets pasted into each site.
    Kept here so the runner/pipeline stay dumb.

    ORDER IS THE FIX. The old builder was world + shot + style, style LAST — and that is
    exactly how aawaaz shipped a photoreal opener in front of 3-D clips: the model latched
    onto the concrete world description first and only saw the style constraint at the very
    end. Now the style LEADS (short, first sentence), the world+shot follow, and the full
    style block is RESTATED verbatim at the end with the anti-photoreal negation attached,
    plus an explicit character lock. Long shots trim the lead back off rather than overflow.

    The WORDS are the same on every site (that is what makes one reel one film). What `site`
    changes is only how they are written down — the shape each box reads best:
      * Vids and Flow (VEO_SITES) get a LABELLED brief, one block per line: cast, action,
        look, dialogue. Both are Veo 3.1, and Veo's own prompting pattern is a structured
        brief, not one long run-on sentence — a labelled block also survives being read by a
        model that skims, and it names the cast and the action in the same place on every clip.
      * every other site keeps the single flowing paragraph it has always been given, so
        nothing that already renders is re-shaped by this.
    A failover moves the whole reel to one site (one reel, one source), so a prompt written
    for the bid site is what the fallback gets too — both Veo sites read either shape, and
    the labelled form is legible to any text-to-video model.
    """
    world = (shot.get("world") or "").strip()
    # The house renders 3-D, always — not the plan's to choose. An animated style wins (it
    # carries the reel's own distinct look); a missing OR non-animated one is replaced by the
    # house anchor, by code, exactly as NO_TEXT is. A photoreal prompt cannot reach a site.
    style = (shot.get("style") or "").strip()
    if not _is_animated(style):
        style = HOUSE_STYLE

    anti = _anti(style)
    lock = _char_lock(shot)                    # ONE string for the reel — appended to every clip
    veo = site in VEO_SITES
    sep = "\n" if veo else ", "
    # Detects extreme close-ups of objects (phone, hands, watch, etc.) — Vids defaults to
    # photorealism for these even with 3-D instructions. When a clip describes a tight shot of
    # an inanimate object, swap the lead to PIXAR-style cartoon language (proven fix for the
    # good-morning-dadaji phone close-up that 2 system re-shoots could not override).
    _PIXAR_LEAD = ("REQUIRED OUTPUT: PIXAR-STYLE CARTOON 3-D OBJECT — chunky rounded "
                   "cartoon proportions, a frame from an animated movie, NOT photoreal, "
                   "NOT real-life object. STYLE for every frame: ")
    _CLOSEUP_OF = re.compile(
        r"\b(?:close[- ]up|extreme close[- ]up|tight shot|macro)\b", re.I)
    _OBJECT_WORDS = re.compile(
        r"\b(?:phone|mobile|smartphone|hand|watch|clock|screen|keyboard|book|cup|"
        r"tablet|laptop|wallet|remote)\b", re.I)
    # A FLASHBACK gets the same treatment, for a different reason. Veo photorealises DISASTER —
    # flood, rain, a man running for his life — whatever the prompt says, and the same prompt
    # renders the calm courtyard clips in clean 3-D (dadaji-ki-dhimi-chal, 2026-10-22: the
    # flashback clip came back live action TWICE, once after the re-shoot, and the generic
    # "REQUIRED OUTPUT: 3-D ANIMATED VIDEO" lead did not hold it). The lead is the one place
    # the site reads FIRST, so it has to name the cartoon form outright for these shots.

    # The medium is the FIRST thing a site reads, so the requirement to render 3-D animation is
    # stated before anything else — catching the medium at generation time, not after.
    lead = (MEDIUM_REQUIREMENT + " STYLE for every frame of this clip, no exceptions: "
            + style.split(".")[0].strip() + ".")
    out = []
    for c in sorted(shot.get("clips", []), key=lambda x: x.get("idx", 0)):
        # per-clip lead swap: an extreme close-up of an object gets PIXAR language
        _p = (c.get("prompt") or "")
        if _CLOSEUP_OF.search(_p) and _OBJECT_WORDS.search(_p):
            _per_clip_lead = _PIXAR_LEAD + style.split(".")[0].strip() + "."
        elif _DISASTER.search(_p):
            _per_clip_lead = CARTOON_LEAD + style.split(".")[0].strip() + "."
        else:
            _per_clip_lead = lead
        says = (c.get("dialogue") or "").strip()
        if says:
            # Narration must come OUT of the clip (WAVELENGTH.md §3), so the line is written into
            # the generation prompt for a character to SPEAK — never laid over at assembly.
            # NOT AS A QUOTED LINE, THOUGH. A quoted Hindi string next to "speaks this line" is
            # read as a CAPTION: dadi-ki-dhansuchi came back with its dialogue burned into the
            # frame as garbled Devanagari subtitles in all four clips — the quote defeated three
            # separate no-text clauses sitting in the same prompt. So the words are handed over as
            # SPEECH the voice carries and the render must never draw: unquoted, and marked as
            # sound. The code-level rules NO_TEXT and _no_text() exist for this same reason —
            # text is kept out by construction, not by the model remembering not to.
            says = ('the character speaks these words aloud in Hindi, on camera, with natural '
                    'lip-sync — no narrator, no voiceover. They are SOUND the voice carries and '
                    'must never be written anywhere: ' + says)
        action = _no_text(_p)
        if veo:
            body = [f"SCENE AND CAST (identical in every clip): {world}" if world else "",
                    f"ACTION AND CAMERA: {action}" if action else "",
                    f"SPOKEN ALOUD, sound only — never written on screen: {says}" if says else ""]
            tail = [f"LOOK, IN EVERY FRAME: {style}", NO_TEXT, anti, _clip_lock(shot, c, lock)]
        else:
            body = [world, action, says]
            tail = [style, NO_TEXT, anti, _clip_lock(shot, c, lock)]
        out.append(_fit(_per_clip_lead, sep.join(x for x in body if x),
                        sep.join(x for x in tail if x), PROMPT_CHARS, sep,
                        floor=MEDIUM_REQUIREMENT))
    return out


# The shot-list contract build_shotlist asks for, enforced in code by validate_plan.
CLIPS_MIN, CLIPS_MAX = 3, 6


def validate_plan(shot: dict, prompts: list = None) -> dict:
    """Lint the plan BEFORE a credit is spent on it (owner fix, 2026-10-14).

    Every item here is a defect that used to be found by a person watching a finished reel —
    four to six generations later, all of them already paid for. `prompts` is what will actually
    be pasted into a site, so a caller cannot lint a plan it is not shipping; when it is not
    given it is built here. bad = refuse to enqueue, warn = the reel can still be made but not
    the one that was asked for.
    """
    bad, warn = [], []
    clips = shot.get("clips") or []
    if not CLIPS_MIN <= len(clips) <= CLIPS_MAX:
        bad.append(f"{len(clips)} clips — a reel is {CLIPS_MIN}-{CLIPS_MAX}")
    idx = [c.get("idx") for c in clips]
    if sorted(idx) != list(range(len(clips))):
        bad.append(f"clip idx should be 0..{len(clips) - 1}, got {idx}")
    for c in clips:
        if not (c.get("prompt") or "").strip():
            bad.append(f"clip {c.get('idx')}: empty prompt")
    if not (shot.get("world") or "").strip():
        bad.append("no world — every clip shares one set, and the identity lock is built from it")
    style = (shot.get("style") or "").strip()
    if not _is_animated(style):
        warn.append("style is missing or not animation — the house 3-D look replaces it, so the "
                    "reel loses the one thing the plan was written to look like")
    if clips and not any((c.get("dialogue") or "").strip() for c in clips):
        warn.append("no clip speaks — the reel will have no narration")
    # THE CLOSING VOICEOVER (the owner's standing rule, 2026-10-22): every reel ends on a line
    # SPOKEN IN THE SCENE — engaging, emotional, relatable. Not a card, not a narrator track: a
    # character says it to camera and it is the last thing said. Linted HERE, before a credit is
    # spent, because the only way to add it later is to buy another generation.
    if clips and not (clips[-1].get("dialogue") or "").strip():
        bad.append("the LAST clip has no spoken line — every reel ends on a closing voiceover "
                   "spoken in the scene (a character to camera, in Hindi). A card cannot carry "
                   "this: the closing line has to be HEARD.")
    prompts = clip_prompts(shot) if prompts is None else prompts
    if len(prompts) != len(clips):
        bad.append(f"{len(prompts)} prompts built for {len(clips)} clips")
    else:
        lock = _char_lock(shot)
        for i, pr in enumerate(prompts):
            if not pr.startswith(MEDIUM_REQUIREMENT[:40]) and not pr.startswith("REQUIRED OUTPUT: PIXAR"):
                bad.append(f"clip {i}: the 3-D medium requirement is gone from the prompt")
            if _DISASTER.search(clips[i].get("prompt") or "") and \
                    not pr.startswith("REQUIRED OUTPUT: PIXAR-STYLE CARTOON 3-D"):
                bad.append(f"clip {i}: a disaster/flashback shot must lead with the CARTOON "
                           "lead — Veo photorealises flood and rain whatever the prompt says")
            if NO_TEXT not in pr:
                bad.append(f"clip {i}: the no-text rule is gone from the prompt")
            if not _anti(style if _is_animated(style) else HOUSE_STYLE) in pr:
                bad.append(f"clip {i}: the anti-photoreal negation is gone from the prompt")
            if _clip_lock(shot, clips[i], lock) not in pr:
                bad.append(f"clip {i}: the identity lock is gone from the prompt")
            if len(pr) > PROMPT_CHARS:
                bad.append(f"clip {i}: {len(pr)} chars overflows the site's box ({PROMPT_CHARS})")
    return {"bad": bad, "warn": warn}


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


def presentation(script: str, shot: dict, prior: list = None, brief: str = "") -> dict:
    """Agent 3: the art director. From the script + shot list it emits the reel's identity and the
    three caption cards (hook, moral, cta) plus what window each card landed in.

    This is the call that produces caption_hook.png / caption_moral.png / caption_cta.png and
    caption_place.json — the artifacts check_craft_layers HELDs a reel on when they are absent, and
    what caption_safe_area / caption_on_face / caption_wording then measure.

    It is stubbed by measure.py and qc.py's selfchecks, so the real path is through pipeline.kickoff."""

    """Agent 3: the reel's IDENTITY and its on-screen caption cards.

    These are the two things the owner had to hand-write per reel (projects/*/identity.json and
    captions.json), which is why the engine could never finish a reel on its own. Both come out
    of one reading of the script, so they are one call.

    `prior` is the list of identities already shipped: WAVELENGTH.md §5 forbids repeating an
    editing pattern, so the new reel has to be TOLD what the old ones did in order to differ.

    `brief` is the owner's OWN text for this reel. Without it the cards are written from the
    script alone, and a brief that states its hook and moral verbatim gets a paraphrase invented
    over the top of it — the one creative decision a human had already made is the one the art
    director silently overrode."""
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
    # The owner's own words, when there are any. A brief that opens with "Hook text: …" and
    # "Moral: …" has already made the two calls agent 3 is about to make, and inventing over
    # them throws away the only human creative input in the whole pipeline.
    brief_block = ""
    if (brief or "").strip():
        brief_block = (
            "\n\nTHE OWNER'S BRIEF — their own words for this reel, which the script above was "
            f"written from:\n{brief.strip()[:4000]}\n"
            "If the brief states a hook line, a moral, an opening title or a closing line in its "
            "own words, THOSE ARE THE WORDS: carry them into the cards, split across the card's "
            "one or two lines and shortened ONLY as far as the character limit forces. Never "
            "replace the owner's line with a paraphrase, a question or a slogan you invent."
        )
    system = _system(
        "You are a reel art-director. You decide how a finished reel LOOKS on screen: its "
        "opening device, its transition language, its typography, and the three caption cards. "
        "Output ONLY valid JSON, no prose."
    )
    user = (
        f"SCRIPT:\n{script}\n{brief_block}\nSHOT PLAN:\n{json.dumps({k: v for k, v in shot.items() if k != 'clips'}, ensure_ascii=False)}\n"
        f"{seen}\n\nProduce JSON with EXACTLY this shape:\n{_PRESENTATION_SCHEMA}\n\nRULES:\n"
        "- The caption cards are read on a phone, muted, in about three seconds each. Every "
        f"line must be under {CARD_CHARS} characters. One or two lines per card, never three.\n"
        "- The hook and the moral cards are written in DEVANAGARI. The CTA may be Hinglish or "
        "the brand name in Roman. Never English-only — see the audience rules above.\n"
        f"- The CTA card carries the foundation's own address, {BRAND_URL}, spelled exactly "
        "like that. There is no other address — do not invent, shorten or guess one.\n"
        "- 'hook' is the opening card (a question, a provocation, or the first line of the "
        "story). 'moral' is the turn — it lands the point. 'cta' is the last card.\n"
        "- If the owner's brief above writes the hook or the moral in its own words, use THOSE "
        "words on the card, not your own version of them.\n"
        "- 'identity.visual_style' must state that this is 3-D animation. This is not optional.\n"
        "- The identity must be DISTINCT from the prior reels listed above.\n"
        "- Return JSON ONLY."
    )
    # ONE REJECTION IS NOT A DEAD END. The contract is strict on purpose — a card that overflows
    # reaches the owner's screen — but no model counts Devanagari characters reliably: handed the
    # owner's own hook line it answered 48 chars, then 45, then 41. A single rejection used to
    # throw the WHOLE art direction away, identity AND every card, over one long line; the reel
    # was then queued with no cards at all and the failure read as one soft line in the kickoff.
    # So the violation is fed back for another go. What is scarce here is the render, not the call.
    last, obj = None, None
    for attempt in range(PRESENTATION_TRIES):
        extra = "" if attempt == 0 else (
            f"\n\nYOUR LAST ANSWER WAS REJECTED — {last}\nWrite the cards so this cannot happen: "
            "split a long line across the card's TWO lines, and drop words only as a last resort. "
            "Keep the owner's wording, and every other field exactly as it was.")
        text, _used = chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user + extra}],
            # 4000, like the shot list: on a reasoning model the budget is spent on thinking
            # before any JSON is written, which truncated the object mid-field at 2000.
            max_tokens=4000, temperature=0.9, role="presentation",
        )
        try:
            obj = _extract_json(text)
        except Exception as e:  # noqa: BLE001 — a bad answer is retried, not fatal
            last, obj = str(e)[:200], None
            continue
        caps = obj.get("captions")
        if isinstance(caps, dict):
            caps["font"] = _font_class(caps.get("font"))
            for card in caps.values():          # the brand's domain is corrected before the judge
                if isinstance(card, dict):      # and the gate ever see the cards
                    card["lines"] = [_fix_url(ln) for ln in card.get("lines") or []]
        try:
            _presentation_selfcheck(obj)
            break
        except Exception as e:  # noqa: BLE001 — feed the violation back and try again
            last, obj = str(e)[:200], None
    if obj is None:
        raise RuntimeError(f"art direction failed {PRESENTATION_TRIES} times: {last}")

    # LAYER 1 of the caption gate: the art director reads its OWN cards back before they are
    # written, with the script in hand. The Hindi is the one layer no metric measures, and the
    # hook is usually not a quote but a TRANSLATION — the script writes its hook in English
    # ("Rich in life because… you stopped by."), so the model is inventing the Devanagari, not
    # copying it. That is how dadi-ki-dhansuchi reached the screen with "उनकी मुस्कान बनें ख़ज़ाना"
    # (बनें is the respectful plural on a feminine singular) and ख़ज़ाना spelled two ways in one
    # reel. The pass is a JUDGEMENT, not a rule — the cards and the script go back to the model and
    # it fixes what a Hindi reader would wince at. Fail-open by design: a gateway that is down, or
    # a "repair" that breaks the card contract, leaves the cards exactly as written, because
    # qc.check_caption_wording is the independent second layer that catches what this one misses.
    _orig_caps = obj["captions"]
    try:
        obj["captions"] = judge_captions(script, _orig_caps)["captions"] or _orig_caps
    except Exception:  # noqa: BLE001 — a review that cannot run must never cost the reel
        obj["captions"] = _orig_caps

    # ---- generate + place the actual card PNGs ----
    # caption_place.json is written by assemble.build AFTER the cut exists, because placement needs
    # the frames the card will sit over. Here we produce the text decisions and the cards themselves
    # from the script + cards; the placement y for each card is filled in once the final cut exists.
    import io
    import os as _os
    import caption
    _project_dir = ((shot.get("slug") or "").strip() and os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects", (shot.get("slug") or "").strip())) or _os.getcwd()
    place = {}
    for kind in ("hook", "moral", "cta"):
        lines = (_orig_caps.get(kind) or _clean_caps.get(kind) or _near_lines.get(kind) or [])
        if not lines:
            continue
        cardpng = _os.path.join(_project_dir, f"caption_{kind}.png")
        try:
            caption.render(lines, cardpng, y=0)
        except Exception as e:
            place[kind] = {"error": str(e)}
        else:
            place[kind] = {"y0": 0, "y1": 0, "x0": 0, "x1": 0, "start": 0.0, "end": 0.0}
    obj["_caption_cards"] = []
    for k in ("hook", "moral", "cta"):
        p = _os.path.join(_project_dir, f"caption_{k}.png")
        if _os.path.exists(p):
            obj["_caption_cards"].append(p)
    obj["_caption_place_template"] = place

    _presentation_selfcheck(obj)            # a repair that overflows a card is no repair
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


# nukta (the dot that turns क/ख/ज/फ into क़/ख़/ज़/फ़) and the zero-width joiners — stripped for
# COMPARISON only, so ख़ज़ाना and खज़ाना land in the same bucket while both surfaces are kept.
_NUKTA = dict.fromkeys(map(ord, "़‌‍"), None)
_DEVA_RUN = re.compile(r"[ऀ-ॿ]+")


def _spelling_variants(lines) -> list:
    """Words the reel writes two different ways — ख़ज़ाना in the hook, खज़ाना in the moral.

    This decides nothing. It hands the judge a fact it would otherwise have to notice by reading
    its own output twice, which is exactly the kind of thing a model skips. Devanagari only: a
    'Standard' vs 'standard' in a CTA is not this defect."""
    seen = {}
    for ln in lines:
        for tok in _DEVA_RUN.findall(ln or ""):
            seen.setdefault(tok.translate(_NUKTA), set()).add(tok)
    return [" / ".join(sorted(v)) for v in seen.values() if len(v) > 1]


_CAPTION_JUDGE = (
    "You are a Hindi copy editor for a short vertical film. You did NOT write these caption "
    "cards. Judge them the way a native speaker watching on a phone, muted, would — then fix them. "
    "Each card is one glance, about three seconds.\n"
    "Work LINE BY LINE, and give every single line of every card its own verdict before you "
    "finish. A line you did not consciously pass is a line you did not check — and the defect this "
    "exists to catch is one that hides in a line that LOOKS fine at a glance.\n"
    "For each line, in this order:\n"
    "- GRAMMAR AND REGISTER: would a Hindi speaker actually say and write this? Verb agreement, "
    "gender, case, idiom. A verbless fragment is wrong. So is a verb inflected for the wrong "
    "gender or number — a respectful-plural ending on a feminine singular noun is exactly that.\n"
    "- CONSISTENCY: one reel, one spelling. The same word written two ways in one reel reads as "
    "carelessness, not as two words.\n"
    "- MEANING: every card carries ONE beat of the script you are given. Does the card say that "
    "beat, in that spirit? Where the script writes the beat in English, the card is a translation "
    "of it, not a new line — a card that quietly changes the claim is wrong even when its Hindi is "
    "flawless.\n"
    "- NATURALNESS: would a reader wince? Fix it if so.\n"
    "Then return the corrected cards. Every problem you name must be VISIBLY fixed in the cards "
    "you return, and every line you changed must appear in problems.\n"
    "Do not touch the 'font' field.\n"
    "Keep every line under 34 characters and 1-2 lines per card. If the cards are already right, "
    "return them unchanged and an empty problems list."
)


def judge_captions(script: str, caps: dict) -> dict:
    """The caption proofread — ONE judge, called from two layers.

    Layer 1 is presentation(): the art director reads its own cards back before writing them.
    Layer 2 is qc.check_caption_wording(): the finished reel's cards are judged again, after the
    render, and a problem gates go_live. Both need the same judgement, so the prompt lives here
    once and each caller decides what to do with the answer — repair (layer 1) or report (layer 2).

    It is fed EVIDENCE, not a rule: the script (the meaning every card must carry), and any word
    the reel spells two ways, computed here so the judge is told rather than left to notice. The
    judgement stays the model's — nothing in this function fires on its own.

    Returns {'problems': [...], 'captions': {...}}. Raises if no gateway answers; the callers
    decide what an outage means (ignored at authoring, a go_live gate at QC)."""
    lines = [ln for k in CARD_KINDS for ln in (caps.get(k) or {}).get("lines") or []]
    variants = _spelling_variants(lines)
    evidence = ""
    if variants:
        evidence = ("\nThis reel spells the same word more than one way — settle on one form:\n- "
                    + "\n- ".join(variants))
    user = (f"SCRIPT:\n{script}\n\nCAPTION CARDS:\n"
            f"{json.dumps(caps, ensure_ascii=False, indent=2)}{evidence}\n\n"
            # The contract goes LAST and says 'do not show your reasoning' on purpose. The free
            # routers answer with reasoning models, and one measured on this call spent 5,045
            # tokens thinking through the cards — reaching the right answer, बनें -> बने — and
            # never emitted the JSON at all, so _extract_json found no '{' and the check read as
            # a gateway outage. A trailing, explicit contract is what actually lands; the same
            # demand buried mid-prompt is what a reasoning model steps over.
            "Answer with JSON ONLY — no reasoning, no commentary, no markdown fences:\n"
            '{"problems": ["one line per thing you fixed or could not fix", ...], '
            '"captions": {<the SAME shape you were given, corrected>}}')
    text, _used = chat(
        [{"role": "system", "content": _system(_CAPTION_JUDGE)},
         {"role": "user", "content": user}],
        # room for the whole card set AND the model's own thinking: a reasoning router that runs
        # out of budget mid-thought returns prose with no JSON in it.
        max_tokens=4000, temperature=0.4,
        # the other end of the gateway order: the writer took whichever answered first, so the
        # judge asks the one it probably did not use — a model that did not write the line. The
        # measured pick for this role leads; `prefer` still decides among the rest.
        prefer=_ORDER[-1] if len(_ORDER) > 1 else None,
        role="judge",
    )
    out = _extract_json(text)
    problems = [str(p).strip() for p in (out.get("problems") or []) if str(p).strip()]
    fixed = out.get("captions")
    if isinstance(fixed, dict):
        fixed["font"] = _font_class(fixed.get("font") or caps.get("font"))
    return {"problems": problems, "captions": fixed}


_PUBLISHING_SCHEMA = """{
  "youtube_title": "the SEARCHABLE title — the words a Hindi speaker would type, front-loaded",
  "youtube_title_alt": "an alternate title, curiosity-led rather than search-led",
  "caption": "the Instagram/Facebook caption: the hook line first, then the caption body",
  "youtube_description": "the YouTube description",
  "tags": "comma-separated YouTube tags",
  "keywords": "comma-separated Instagram/Facebook keywords",
  "hashtags": "#hashtags for IG/FB"
}"""


def publishing_copy(facts: dict) -> dict:
    """The reel's publishing sheet — the words it goes out with, written from the reel itself.

    The owner's template folder (dadaji-ka-kahani, 2026-10-14) carried a hand-written
    publishing.md: titles, a caption, a description, per-platform keywords and hashtags. That is
    copywriting, which is what the other three agents already do, so it comes from the same brain
    and reads the same wavelength and craft lessons (`_system`). Independent of the render: an
    outage here must never cost a reel, so the caller falls back to the reel's own words.

    `facts` is the reel's OWN text — title, script, the caption cards, the shot plan's world, the
    duration. Nothing is invented that is not in there, and the hook line is returned verbatim so
    the profile grid and the reel's first frame say the same thing."""
    system = _system(
        "You write the publishing copy for a finished Hindi short reel for an Indian senior-care "
        "NGO. It is posted to YouTube Shorts, Instagram Reels and Facebook. You write ONLY the "
        "words it ships with. Output ONLY valid JSON, no prose."
    )
    hook_lines = [l for l in ((facts.get("cards") or {}).get("hook", {}).get("lines") or [])
                  if l.strip()]
    # The grid shows the reel's FIRST line — so the caption must open on that one line, not on the
    # whole card. A card split over two lines used to be joined with " / " here, and the model was
    # asked to repeat a join that is not a line of the reel: it never did, and the sheet fell back
    # to the reel's own words every time. The card is given as its lines; the check takes the first.
    hook = hook_lines[0] if hook_lines else ""
    user = (
        f"REEL TITLE: {facts.get('title', '')}\n"
        f"LENGTH: {facts.get('seconds', 0):.0f} seconds, vertical 9:16\n"
        f"THE REEL'S OWN OPENING CARD (verbatim, one line per line):\n"
        + "".join(f"  {i+1}. {l}\n" for i, l in enumerate(hook_lines))
        + "\n"
        f"SCRIPT:\n{facts.get('script', '')}\n\n"
        f"CAPTION CARDS:\n{json.dumps(facts.get('cards') or {}, ensure_ascii=False)}\n\n"
        f"WHAT IT IS (from the shot plan):\n{facts.get('plan', '')}\n\n"
        f"Produce JSON with EXACTLY this shape:\n{_PUBLISHING_SCHEMA}\n\nRULES:\n"
        "- Devanagari Hindi is the language of the audience. The titles, the caption and the "
        "description are in Hindi (a Roman brand name or hashtag is fine inside Hindi text).\n"
        "- 'youtube_title' front-loads what someone would actually TYPE to find this; keep it "
        "under 90 characters.\n"
        "- 'caption' MUST begin with the opening card's FIRST line VERBATIM, then a blank line, "
        "then the body. The profile grid shows the first line, so it has to be the reel's own "
        "promise. Do not copy the line numbers or join the lines together.\n"
        "- The caption body: 2-4 short paragraphs or bullets drawn from the reel's actual lines, "
        "then the moral, then ONE question that invites a comment, then the foundation's address "
        f"{BRAND_URL} — spelled exactly like that, no other address, never invented or shortened.\n"
        "- 'tags' and 'keywords' are comma-separated, 10-14 entries, covering what this reel is "
        "about in the words people search. 'hashtags' is 12-15 hashtags, #-prefixed.\n"
        "- Say only what the reel says. Never a statistic, a claim, a date or a promise that is "
        "not in the text above.\n"
        "- Return JSON ONLY."
    )
    text, _used = chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=3000, temperature=0.8, role="publishing",
    )
    out = _extract_json(text)
    for k in ("youtube_title", "youtube_title_alt", "caption", "youtube_description"):
        if isinstance(out.get(k), str):
            out[k] = _fix_url(out[k])
    for k in ("tags", "keywords", "hashtags"):
        if isinstance(out.get(k), list):
            out[k] = ", ".join(str(x) for x in out[k])
    _publishing_selfcheck(out, hook)
    return out


def _publishing_selfcheck(obj: dict, hook: str = "") -> bool:
    """OFFLINE contract for publishing_copy(). A sheet with no hook line, a hallucinated address
    or no hashtags is worse than no sheet — it would go out. Asserted here, where it is free."""
    for k in ("youtube_title", "caption", "youtube_description", "tags", "keywords", "hashtags"):
        assert isinstance(obj.get(k), str) and obj[k].strip(), f"publishing: missing '{k}'"
    if hook:
        assert obj["caption"].strip().startswith(hook.strip()), \
            "publishing: the caption must open with the reel's own hook line, verbatim"
    assert BRAND_URL in obj["caption"] or BRAND_URL in obj["youtube_description"], \
        "publishing: the foundation's address is missing from the copy"
    assert re.search(r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9-]+\.(?:org|com|in|net|co|io)\S*",
                     obj["caption"]).group(0).startswith(BRAND_URL), \
        "publishing: a different domain reached the copy"
    assert len(obj["hashtags"].split()) >= 8, "publishing: too few hashtags"
    return True


def finalize_caption_placement(obj: dict, video: str, windows=None) -> dict:
    """Fill caption_place.json from the frozen cut, using the card PNGs produced in presentation.

    presentation only wrote the cards and a zero-placeholder place; this is the step that needs the
    actual frames. Called by assemble.build after the cut is stitched; idempotent — a reel that has
    already been placed keeps its existing windows and only fills any still-empty ones.

    `windows` maps kind -> (start, end) in SECONDS of the shipped cut. Without it nothing is
    invented: every card gets an honest error entry instead of a fabricated window. Before this
    argument existed the function sampled the whole reel (its 0.05–0.95 fractions) and then wrote
    THOSE NUMBERS into the record as if they were screen times — every card claimed the same
    overlapping second, corrupting the placement and every check that reads it
    (diwali-purana-sandook, 2026-10-23). A placement nobody named is an error, not one second.

    The merge only FILLS: an existing valid window is a previous placement's own answer and
    silently overwriting it here corrupted `caption_place.json` after assemble.build had already
    written the real post-pace windows.
    """
    import json as _json
    import os as _os
    import caption as _caption
    pdir = obj.get("_project_dir") or _os.getcwd()
    cards = obj.get("_caption_place_template") or {}
    windows = windows or {}
    out = {}
    for kind, tpl in cards.items():
        if "error" in tpl:
            out[kind] = {"error": tpl["error"]}
            continue
        png = _os.path.join(pdir, f"caption_{kind}.png")
        if not _os.path.exists(png):
            out[kind] = {"error": f"card not found: caption_{kind}.png"}
            continue
        im = __import__("PIL").Image.open(png).convert("RGBA")
        rows = __import__("numpy").where(__import__("numpy").asarray(im)[..., 3] > 150)
        if not len(rows[0]):
            out[kind] = {"error": "card has no opaque pixels"}
            continue
        top, bot = int(rows[0].min()), int(rows[0].max())
        card_h = bot - top + 1
        win = windows.get(kind)
        if not win or len(win) < 2:
            out[kind] = {"error": "no screen window given — placement needs the caller to name "
                                  "the cut's own times; none were invented"}
            continue
        t0, t1 = float(win[0]), float(win[1])
        if t1 <= t0 or t0 < -1e-9:
            out[kind] = {"error": f"bad screen window given for {kind}: ({t0}, {t1})"}
            continue
        y = _caption.find_empty_band(video, t0, t1, card_h)
        if y is None:
            out[kind] = {"error": f"no empty band found for {kind} in ({t0:.2f}, {t1:.2f})s"}
            continue
        out[kind] = {"x0": 0, "y0": y, "x1": 560, "y1": y + card_h,
                     "start": round(t0, 3), "end": round(t1, 3)}
    place_p = _os.path.join(pdir, "caption_place.json")
    existing = {}
    if _os.path.exists(place_p):
        try:
            existing = _json.load(open(place_p, encoding="utf-8"))
        except Exception:
            pass
    # fill, never overwrite: an existing valid window already names where its card is, and this
    # step has no standing to second-guess it
    merged = dict(existing)
    for k, v in out.items():
        old = merged.get(k)
        is_placed = isinstance(old, dict) and "start" in old and "end" in old and not old.get("error")
        if not is_placed:
            merged[k] = v
    _json.dump(merged, open(place_p, "w", encoding="utf-8"), indent=2)
    return merged


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
    """LLMs wrap JSON in fences or reasoning; grab the first balanced object.

    strict=False because models put a LITERAL newline or tab inside a string — a raw control
    character is invalid JSON, and it took the whole kickoff down as a traceback (agent 2 had
    written a multi-line prompt). The character is what the model meant; refusing the answer
    over it costs a run and teaches nothing."""
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
                return json.loads(text[start:i + 1], strict=False)
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
    for i, pr in enumerate(prompts):
        # A DISASTER shot leads with the CARTOON form of the same requirement — Veo photorealises
        # flood and rain under the generic lead (dhimi-chal, 2026-10-22, twice). Both leads carry
        # the 3-D requirement; only the wording differs, and the shot decides which.
        _disaster = _DISASTER.search(shot["clips"][i].get("prompt") or "")
        _lead_ok = (pr.startswith(MEDIUM_REQUIREMENT) if not _disaster
                    else pr.startswith(CARTOON_LEAD))
        assert _lead_ok, \
            f"clip {i}: the 3-D requirement must LEAD every prompt — the medium is caught at " \
            "generation time, before a photoreal frame is ever rendered"
        assert "style for every frame" in pr.lower(), \
            f"clip {i}: the style anchor must follow the requirement — appended last, an early " \
            "shot drifts photoreal"
        assert world[:15].lower() in pr.lower(), f"clip {i}: world anchor must be present"
        assert shot["clips"][i]["prompt"][:20].lower() in pr.lower(), \
            f"clip {i}: the shot description must be present"
        assert "NOT photorealistic" in pr, \
            f"clip {i}: the anti-photoreal negation must be attached"
        assert NO_TEXT in pr, \
            f"clip {i}: the no-text rule must reach EVERY prompt — the model cannot be trusted " \
            "to remember it, so clip_prompts attaches it by code"
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
    lock = _char_lock(shot)
    assert lock, "a reel with a cast must get an identity lock"
    # A FLASHBACK clip carries the reel's lock in its flashback form (same person, younger, in
    # that day's clothes) — the standard lock would hand the generator two opposite orders about
    # the one thing the shot is about, and it would pick one (dadaji-ki-yadein, 2026-10-19). So
    # the check accepts EITHER form, and demands that the one in the prompt is the right one for
    # that clip: the standard lock everywhere else, the flashback form exactly on the clips whose
    # own text declares the earlier time.
    for i, pr in enumerate(prompts):
        _want = _clip_lock(shot, shot["clips"][i], lock)
        assert _want in pr, \
            f"clip {i}: the identity lock must sit in EVERY clip prompt — the first scene and " \
            "the last are the same person only if every clip is told the same thing"
    assert all(c in lock for c in _char_clauses(world)), \
        "the lock must quote the character's own description verbatim, not just their tag"
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
        _ns = clip_prompts(_nostyle)
        assert len(_ns) == 3, "a styleless plan must still build every clip"
        assert all(HOUSE_STYLE.lower() in p.lower() for p in _ns), \
            "a styleless plan must be filled in with the house 3-D anchor, not left bare"
        # A plan that asks for PHOTOREAL is replaced by the house 3-D anchor by code (owner rule
        # 2026-10-10: every Standard Sunrise reel is 3-D). This is the generation-side half of the
        # dadaji-ka-kahani fix; qc._declared_medium is the gate-side half.
        _photo = dict(_shot, style="warm amber photoreal render, lifelike grandfather")
        _pp = clip_prompts(_photo)
        assert all("NOT photorealistic" in p for p in _pp), \
            "a photoreal plan must be forced into the house 3-D anchor, anti-photoreal attached"
        assert all(HOUSE_STYLE.lower() in p.lower() for p in _pp), \
            "the house 3-D style must fill in for a non-animated plan"
        # The medium is caught at generation time: EVERY prompt LEADS with the 3-D requirement,
        # before the style block and the shot — this is what gives the site a chance to render
        # animation the first time (owner rule 2026-10-10).
        assert all(p.startswith(MEDIUM_REQUIREMENT) for p in clip_prompts(_shot)), \
            "every clip prompt must lead with the 3-D requirement, not just describe the scene"
        # The film carries no text of its own: a shot-lister that writes "a glowing 'एक' appears
        # in the notebook" ASKS the generator for exactly the baked-in text NO_TEXT forbids — two
        # opposite orders in one prompt. clip_prompts strips the request by code, so the frame
        # cannot come back with gibberish Devanagari (the owner's own rule: not a model's memory).
        _texty = dict(_shot, clips=[{"idx": 0,
                                     "prompt": ("She opens the notebook; a glowing \"एक\" appears on "
                                                "the page. The camera pushes in slowly."),
                                     "dialogue": "एक अजनबी ने गाया।"}])
        _tp = clip_prompts(_texty)[0]
        assert "glowing" not in _tp.lower() and "appears on the page" not in _tp.lower(), \
            "an authored request to write text into the frame must be stripped before generation"
        assert "notebook" in _tp.lower() and "pushes in" in _tp.lower(), \
            "only the text-asking sentence is dropped — the rest of the shot must survive"
        assert NO_TEXT in _tp, "the code-level no-text rule must still be attached"
        # A DISASTER shot leads with the CARTOON lead. Veo photorealises flood/rain/storm
        # whatever the prompt says — the generic 3-D lead did not hold the dhimi-chal flashback
        # (it came back live action twice, including on a re-shoot with the rejection folded in).
        _flood = dict(_shot, clips=[{"idx": 0,
                                     "prompt": ("Flashback: a young man runs through a "
                                                "flood-filled village path, water up to his knees, "
                                                "heavy rain falling."),
                                     "dialogue": ""}])
        _fp = clip_prompts(_flood)[0]
        assert _fp.startswith("REQUIRED OUTPUT: PIXAR-STYLE CARTOON 3-D"), \
            f"a flood/flashback shot must lead with the CARTOON lead, got: {_fp[:60]!r}"
        assert "NOT photoreal" in _fp, "the cartoon lead must negate photoreal outright"
        _calm = clip_prompts(dict(_shot, clips=[{"idx": 0, "prompt": "He smiles in the courtyard.",
                                                   "dialogue": ""}]))[0]
        assert _calm.startswith(MEDIUM_REQUIREMENT), \
            "a calm shot must keep the ordinary 3-D lead — the cartoon lead is not the default"
        # The spoken line rides along as SOUND, never as a quoted string a renderer can typeset.
        # dadi-ki-dhansuchi came back with its dialogue burned into all four clips as garbled
        # Devanagari subtitles — a quoted Hindi line beside "speaks this line" reads as a caption.
        assert '"' not in _tp and "“" not in _tp, \
            "the spoken line must never be quoted — a quoted line is rendered as a subtitle"
        assert "एक अजनबी ने गाया।" in _tp, "the words must still reach the site to be spoken"
        assert "SOUND" in _tp, "the words must be marked as speech for the voice, not text for the eye"
        # The medium is decided at the START: a shot list whose style does not read as animation
        # is repaired at build time (chat stubbed), so the plan is honest before any clip is queued.
        _orig_chat = chat
        try:
            globals()["chat"] = lambda *a, **k: (json.dumps({
                "title": "t", "slug": "s", "world": "A room. BABAJI sits.",
                "style": "warm amber photoreal render, lifelike grandfather",
                "music_mood": "warm", "music_want": "warm", "aspect": "9:16",
                "clips": [{"idx": 0, "prompt": "He smiles.", "dialogue": ""}]}), "stub")
            _sl = build_shotlist("script")
            assert _is_animated(_sl["style"]), \
                "build_shotlist must repair a non-animated style to the house 3-D anchor"
            assert _sl["style"] == HOUSE_STYLE, "the repair uses the house anchor verbatim"
        finally:
            globals()["chat"] = _orig_chat
        # An unreadable shot list is RETRIED, not fatal. A literal newline inside a string is
        # invalid JSON and it took the whole kickoff down as a traceback — a lost run over a
        # control character, from the one call in the pipeline that buys nothing.
        _good = json.dumps({"title": "t", "slug": "s", "world": "A room. BABAJI sits.",
                            "style": "soft stylized 3-D animation", "music_mood": "warm",
                            "music_want": "warm", "aspect": "9:16",
                            "clips": [{"idx": 0, "prompt": "He smiles.", "dialogue": ""}]})
        _shots, _calls = {"n": 0}, []
        def _stub_broken(msgs, **kw):
            _calls.append(msgs[-1]["content"])
            _shots["n"] += 1
            return ("{not json at all", _good)[_shots["n"] > 1], "stub"
        globals()["chat"] = _stub_broken
        try:
            assert build_shotlist("script")["world"] == "A room. BABAJI sits.", \
                "a shot list that came back unreadable must be asked for again"
            assert _shots["n"] == 2, f"exactly one retry, got {_shots['n']}"
            assert "NOT VALID JSON" in _calls[-1], "the retry must say what was wrong"
        finally:
            globals()["chat"] = _orig_chat
        # …and a literal control character inside a string is READ, not rejected: the model means
        # a newline, and the answer is otherwise well-formed.
        assert _extract_json('{"a": "one\ntwo"}')["a"] == "one\ntwo", \
            "a raw control character in a string must not throw the answer away"
        # a very long plan must be trimmed to fit, never silently overflow
        _big = dict(_shot, world=_shot["world"] + " " + ("extra detail " * 400))
        assert all(len(pr) <= PROMPT_CHARS for pr in clip_prompts(_big)), \
            "an oversized plan must be trimmed to PROMPT_CHARS"
        # …and the trim must cut the SHOT, never the identity lock. Trimming a prompt's tail is
        # how a lock disappears off the last clip of a long reel — which is exactly the clip the
        # face changes in.
        _blk = _char_lock(_big)
        assert _blk and all(_blk in pr for pr in clip_prompts(_big)), \
            "the identity lock must survive the length trim — the shot gives, the lock does not"
        # A FLASHBACK CLIP CANNOT CARRY THE STANDARD LOCK. The action line says "Flashback: young
        # Dadaji" while the lock orders the SAME elderly face in the SAME clothes — two opposite
        # orders in one prompt, and the generator obeys whichever it likes (dadaji-ki-yadein:
        # the young-clip came back old, or the old clips came back young).
        _fb = dict(_shot, clips=[dict(_shot["clips"][0], prompt="Flashback: a young man runs."),
                                 _shot["clips"][1], _shot["clips"][2]])
        _fp = clip_prompts(_fb)
        _std, _fl = _clip_lock(_fb, _fb["clips"][1]), _clip_lock(_fb, _fb["clips"][0])
        assert _fl != _std and _fl in _fp[0] and _std not in _fp[0], \
            "a clip that declares a flashback must get the younger lock, not the standard one"
        assert _std in _fp[1] and _std in _fp[2], \
            "the jump is per-clip: a plain clip keeps the standard lock"
        assert "YOUNGER" in _fl and "same face structure" in _fl, \
            "the flashback lock drops age, never identity"
        assert declares_flashback("Back to present day.") is False, \
            "'back to present' is the RETURN from the jump — marking it moves the exclusion " \
            "onto the wrong clip"
        # …and so must the medium requirement. When the guarantees alone nearly fill the box the
        # old trim dropped the lead WHOLE — MEDIUM_REQUIREMENT, the rule that the frame is 3-D at
        # all, went with it and the clip was free to render photoreal. The lead has a floor now.
        _fat = dict(_shot, style="Soft stylized 3-D animation. " + ("very detailed render note. " * 120))
        assert all(pr.startswith(MEDIUM_REQUIREMENT) for pr in clip_prompts(_fat, "googlevids")), \
            "a prompt whose tail fills the box must still lead with the medium requirement"
        assert any("no-text" in b for b in validate_plan(_fat)["bad"]), \
            "a plan whose own style eats the box must be REFUSED before it is queued, not " \
            "shipped with its no-text rule and identity lock cut off the end"

        # THE PLAN LINT. These are the defects that used to be found by a person watching the
        # finished reel, four to six paid-for generations later.
        assert not validate_plan(_shot)["bad"], "a well-formed plan must pass the lint"
        assert any("clips" in b
                   for b in validate_plan(dict(_shot, clips=_shot["clips"][:2]))["bad"]), \
            "a two-clip plan must be refused before it is queued, not after it renders"
        assert any("idx" in b for b in validate_plan(dict(_shot, clips=[
            {"idx": 0, "prompt": "one"}, {"idx": 3, "prompt": "two"},
            {"idx": 4, "prompt": "three"}]))["bad"]), "idx must be a continuous 0..n-1"
        assert any("world" in b for b in validate_plan(dict(_shot, world=""))["bad"]), \
            "a plan with no shared world has no identity lock either"
        _nw = validate_plan(dict(_shot, style="photoreal live action, real film grain"))
        assert not _nw["bad"] and any("style" in w for w in _nw["warn"]), \
            "code repairs a non-animated style, so it is a warning, not a refusal"
        # the lint has to BITE on what it exists to catch — a prompt that lost a guarantee
        _stripped = [p.replace(MEDIUM_REQUIREMENT, "", 1) for p in clip_prompts(_shot)]
        assert any("medium" in b for b in validate_plan(_shot, _stripped)["bad"]), \
            "the lint must catch a prompt that lost the 3-D rule — otherwise it is decoration"

        # --- the SITE SHAPE: same words, written the way Vids and Flow read a prompt ---------
        # Vids and Flow are both Veo 3.1 and the waterfall's two primaries, so they get a
        # labelled brief; every other site keeps the single flowing paragraph it already had.
        _v = clip_prompts(_shot, "googlevids")
        _f = clip_prompts(_shot, "flow")
        _p = clip_prompts(_shot, "kling")
        assert _v == _f, "Vids and Flow are the same engine — one pattern, not two"
        assert "\n" in _v[0] and "\n" not in _p[0], \
            "the Veo sites get a labelled brief, the rest the one-line paragraph they always had"
        for i, pr in enumerate(_v):
            assert pr.startswith(MEDIUM_REQUIREMENT), \
                f"clip {i}: the 3-D requirement must still LEAD the brief"
            assert "SCENE AND CAST" in pr and "ACTION AND CAMERA" in pr, \
                f"clip {i}: the brief must name the cast and the action in the same place " \
                "on every clip — that is what holds a face steady from the first scene to the last"
            assert _char_lock(_shot) in pr, f"clip {i}: the brief must carry the identity lock"
            assert NO_TEXT in pr, f"clip {i}: the brief must carry the no-text rule"
            assert len(pr) <= PROMPT_CHARS, f"clip {i}: the brief may not overflow the site's box"
        assert all(_char_lock(_shot) in pr for pr in clip_prompts(_big, "googlevids")), \
            "the brief must survive the trim with its lock intact"
        # a plan the model wrote with no ALL-CAPS tag still gets a lock — the hole that let a
        # reel ship with three different women playing the same character
        _note = dict(_shot, world="An old-age home at night. A frail old man in a mustard kurta.")
        assert _char_lock(_note), "a cast with no ALL-CAPS tag must still be locked"
        assert all(_char_lock(_note) in pr for pr in clip_prompts(_note, "flow")), \
            "the fallback lock must reach every clip prompt too"
        # …and the fallback quotes the CAST, not the room. `world` is written setting-first,
        # subject-LAST, so taking the opening sentence locked the room and never described the
        # man — the lock said "the characters are exactly these: A cozy living-room…", which is
        # the same hole the tag-less fallback exists to close (dadaji-ka-kahani shipped it).
        _room = {"world": "A cozy living-room at dusk, amber light through the curtains. "
                          "The recurring subject is an elderly grandfather in a white kurta."}
        assert "grandfather" in _char_lock(_room), \
            "a `world` with no ALL-CAPS tag must lock the SUBJECT, not the room it sits in"
        print("OK: one identity lock per reel, quoted from the character's own description and "
              "repeated VERBATIM on every clip — and it survives the length trim")
        # a FLASHBACK clip carries the lock in its flashback form — the same person, younger, in
        # that day's clothes. The reel's own check must accept it: it demanded the standard lock
        # everywhere, so any plan with a flashback failed the check outright.
        _fb = {"world": "The recurring character is an elderly Indian grandfather, about 70, thin, "
                        "balding, wearing a simple teal-green khadi kurta and cotton pajama, no "
                        "glasses, no hat, no jewellery, no accessories.",
               "style": "soft warm morning palette, rounded 3-D animation, shallow depth of field, "
                        "global illumination.",
               "clips": [{"idx": 0, "prompt": "Dadaji walks slowly down the lane."},
                         {"idx": 1, "prompt": "Flashback: young Dadaji in 1975 runs through the "
                                               "flood water, soaked to the waist."}]}
        _fbp = clip_prompts(_fb)
        assert _char_lock(_fb) in _fbp[0], "a normal clip keeps the reel's standard lock"
        assert "FLASHBACK — THE SAME PERSON" in _fbp[1], \
            "a flashback clip must carry the lock in its flashback form"
        assert _char_lock(_fb) not in _fbp[1], \
            "a flashback clip must not carry the standard lock — it contradicts the shot"
        _prompt_selfcheck(_fb)
        print("OK: a flashback clip carries the identity lock in its flashback form, and the "
              "reel's own check accepts it")
        print("OK: Vids and Flow get the labelled Veo brief, every other site the plain "
              "paragraph — the same words either way")
        print("OK: clip_prompts LEADS with the 3-D requirement, then the style anchor (restated), "
              "negates photorealism, locks every character, writes each shot's spoken line INTO "
              "the prompt (narration comes out of the clip), and fits the input")
        print("OK: a styleless or photoreal plan — and a photoreal shot list — is filled in with "
              "the house 3-D anchor at the START; an oversized plan is trimmed, not dropped")

        # --- the pre-generation trigger: the brain's learned rules reach the system prompt ---
        # This is the owner's ask — the brain must fire BEFORE a generation, not only audit after.
        _b = _brief()
        if _b:
            assert "already learned" in _b.lower(), "the brief must read as rules, not raw rows"
            assert _brief() is _b, "the brief is cached — one db read per process, not per call"
            assert _b in _system("ROLE TEXT"), \
                "the learned rules must sit in FRONT of every agent's system prompt"
        # a film rule reaches the brief; a PROCESS rule must NOT (it is not about the film)
        import state as _state_mod
        assert "verify-untracked-before-commit" in _state_mod.OFF_FILM, \
            "a commit-hygiene rule must be classified as off-film"
        print(f"OK: the brain briefs every agent before it writes ({len(_b)} chars of learned rules)"
              if _b else "OK: no brain present — agents still run (fail-open)")

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

        # --- layer 1 of the caption gate: the art director reads its own Hindi back ---
        # First the EVIDENCE, computed here so the judge is told rather than left to notice:
        # ख़ज़ाना in one card and खज़ाना in another are one word spelled two ways. Nukta is
        # stripped for the comparison only, so both surfaces survive into the message.
        _two_way = ["आपका एक मिनट उनका ख़ज़ाना है", "उनकी मुस्कान बनें खज़ाना"]
        assert _spelling_variants(_two_way) == ["खज़ाना / ख़ज़ाना"], _spelling_variants(_two_way)
        assert _spelling_variants(["एक ही शब्द", "एक और शब्द"]) == [], \
            "two different words must not read as two spellings of one"
        assert _spelling_variants(["Standard Sunrise Foundation"]) == [], \
            "a Roman CTA is not a Devanagari spelling defect"
        # Then the judge itself. It is a model call, so prove the CONTRACT offline with the
        # gateway stubbed — the corrected cards come back, and the script reaches the prompt.
        _orig_chat2 = chat
        _seen = {}
        def _stub_chat(msgs, **kw):
            _seen["user"], _seen["prefer"] = msgs[-1]["content"], kw.get("prefer")
            return json.dumps({
                "problems": ["बनें is the respectful plural on मुस्कान (feminine) — use बने"],
                "captions": {"font": "rounded",
                             "hook": {"lines": ["क्या आप उनका एक मिनट देंगे?"]},
                             "moral": {"lines": ["आपका एक मिनट उनका ख़ज़ाना है"]},
                             "cta": {"lines": ["Standard Sunrise Foundation"]}}}), "stub"
        globals()["chat"] = _stub_chat
        try:
            _j = judge_captions("HOOK: Rich in life because… you stopped by.", _ident()["captions"])
            assert _j["problems"], "a judged-wrong card must come back with its problems"
            assert _j["captions"]["hook"]["lines"] == ["क्या आप उनका एक मिनट देंगे?"], \
                "the CORRECTED cards are what layer 1 adopts — a flag with no repair fixes nothing"
            assert "Rich in life because" in _seen["user"], \
                "the script must be handed to the judge — meaning is judged against it"
            assert _seen["prefer"], \
                "the judge must prefer a gateway other than the one that wrote the cards"
        finally:
            globals()["chat"] = _orig_chat2
        print("OK: the caption judge is handed the script and any two-way spelling, and prefers "
              "the other gateway; it returns the cards CORRECTED, not merely flagged")

        # ROUTER HEALTH. A walled route must be SKIPPED by the next call, not re-discovered: the
        # retry budget (3 tries + backoff) exists for a busy tier, and paying it again on every
        # one of a run's dozens of calls is where a 429 turns into minutes. Proven at the seam —
        # a stubbed urlopen that counts how many times each route is actually knocked on.
        import routerhealth as _rh
        _rh.clear()
        _tries = {}
        _sent = {}
        _orig_urlopen = urllib.request.urlopen
        _real_gw = dict(_GATEWAYS)
        try:
            def _fake_open(req, timeout=None):
                name = "walled" if "walled" in req.full_url else "good"
                _tries[name] = _tries.get(name, 0) + 1
                _sent[name] = json.loads((req.data or b"{}").decode()).get("model")
                if name == "walled":
                    raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
                class _R:
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                    def read(self, *_a):
                        return json.dumps({"model": "m",
                                           "choices": [{"message": {"content": "hi"}}]}).encode()
                return _R()
            urllib.request.urlopen = _fake_open
            _GATEWAYS.clear()
            _GATEWAYS.update({"walled": ("http://walled", "k", "m", 1),
                              "good": ("http://good", "k", "m", 1)})
            _orig_order = _ORDER[:]
            _ORDER[:] = ["walled", "good"]
            _t1 = chat([{"role": "user", "content": "x"}])
            assert _t1[0] == "hi" and _tries["walled"] == int(_TRIES) and _tries["good"] == 1, \
                f"the first call pays the retry budget and moves on: {_tries}"
            _tries.clear()
            _t2 = chat([{"role": "user", "content": "y"}])
            assert _t2[0] == "hi", "a cooling route must not break the call"
            assert _tries.get("walled", 0) == 0, \
                f"a WALLED route must be skipped by the next call, not re-knocked: {_tries}"
            # THE MEASURED ASSIGNMENT. A role with a pick leads with the picked route AND the
            # picked model — measured per role, not guessed, and the rest of the order still runs
            # behind it so a subordinate takes over if the winner is walled.
            _rh.clear()
            _tries.clear(); _sent.clear()
            _orig_picks = _picks
            try:
                globals()["_picks"] = lambda: {"z": {"route": "good", "model": "pinned-1"}}
                assert chat([{"role": "user", "content": "w"}], role="z")[0] == "hi"
                assert next(iter(_tries)) == "good", \
                    f"the MEASURED pick must lead the order, not sit behind LLM_ORDER: {_tries}"
                assert _sent["good"] == "pinned-1", \
                    f"the pick's model must be the one sent, got {_sent['good']!r}"
                _tries.clear(); _sent.clear()
                chat([{"role": "user", "content": "x"}])       # no role -> the plain order
                assert next(iter(_tries)) == "walled", \
                    "a call with no role must keep the plain LLM_ORDER"
            finally:
                globals()["_picks"] = _orig_picks
            # …and the picks FILE is what feeds it: read at call time, re-read when it changes.
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as _f:
                json.dump({"w": {"route": "good", "model": "pinned-2"}}, _f)
                _pf = _f.name
            _orig_md, _orig_cache = _PICKS_MD, _picks_cache
            try:
                _PICKS_MD = _pf
                _picks_cache = (0.0, {})
                assert _picks().get("w", {}).get("model") == "pinned-2", \
                    "the pick written by measure.py must be read"
                os.unlink(_pf)
                with open(_pf, "w") as _f:
                    _f.write("{ not json")
                _picks_cache = (0.0, {})           # a corrupt file must not break a run
                assert _picks() == {} or _picks().get("w") is None, \
                    "an unreadable picks file must fall back to the plain order, never raise"
            finally:
                _PICKS_MD, _picks_cache = _orig_md, _orig_cache
                if os.path.exists(_pf):
                    os.unlink(_pf)
            # …and the memory must never be the reason a run fails: with EVERY route cooling,
            # they are all tried anyway rather than raising 'nothing to talk to'.
            assert _rh.cooling("good")[0] == 0, "the route that answered must be healthy"
            _rh.note_failure("good", "429 too many requests")
            _tries.clear()
            assert chat([{"role": "user", "content": "z"}])[0] == "hi", \
                "an all-cooling fleet must still be tried — a memory of failure is not a failure"
            assert _tries.get("good", 0) >= 1, "the last resort must actually be attempted"
        finally:
            urllib.request.urlopen = _orig_urlopen
            _GATEWAYS.clear(); _GATEWAYS.update(_real_gw)
            _ORDER[:] = _orig_order
            _rh.clear()
        print("OK: a 429 walls a route for the following calls; the fleet is still tried when "
              "everything is cooling, so the memory can never itself fail a run")

        # presentation() must WRITE the corrected cards (layer 1 is a fix, not a flag) and must
        # fail OPEN when the judge cannot run — a caption review must never cost the reel.
        _FIXED = dict(_ident()["captions"], hook={"lines": ["क्या आप उनका एक मिनट देंगे?"]})
        def _stub_pres(msgs, **kw):
            if "CAPTION CARDS" in msgs[-1]["content"]:           # the judge's own call
                return json.dumps({"problems": ["बनें -> बने"], "captions": _FIXED}), "stub"
            return json.dumps(_ident()), "stub"                  # the art director's call
        globals()["chat"] = _stub_pres
        try:
            assert presentation("script", _shot)["captions"]["hook"]["lines"] == \
                ["क्या आप उनका एक मिनट देंगे?"], \
                "presentation must ADOPT the corrected cards, not merely receive them"
            def _boom(msgs, **kw):
                if "CAPTION CARDS" in msgs[-1]["content"]:
                    raise RuntimeError("all LLM gateways failed")
                return json.dumps(_ident()), "stub"
            globals()["chat"] = _boom
            assert presentation("script", _shot)["captions"]["hook"]["lines"] == \
                _ident()["captions"]["hook"]["lines"], \
                "a judge that cannot run must leave the cards as written — fail OPEN"
        finally:
            globals()["chat"] = _orig_chat2
        print("OK: presentation adopts a caption repair, and fails open when the judge is down")

        # The OWNER'S BRIEF reaches agent 3. Without it the art director writes a hook over the
        # top of one the owner already wrote — a brief that opens "Hook text: …" and "Moral: …"
        # had both cards replaced by invented questions, which is the one creative call in the
        # pipeline a human had already made.
        _seen = {}
        def _stub_brief(msgs, **kw):
            if "CAPTION CARDS" not in msgs[-1]["content"]:
                _seen["user"] = msgs[-1]["content"]
            return json.dumps(_ident()), "stub"
        globals()["chat"] = _stub_brief
        try:
            presentation("script", _shot, brief="Hook text: दादाजी रोज़ सुबह 5 बजे भेजते हैं")
            assert "दादाजी रोज़ सुबह 5 बजे भेजते हैं" in _seen.get("user", ""), \
                "the owner's own words must reach the art director, not just the script"
            _seen.clear()
            presentation("script", _shot)
            assert "OWNER'S BRIEF" not in _seen.get("user", ""), \
                "a caller that passes no brief must not be told about one"
        finally:
            globals()["chat"] = _orig_chat2
        print("OK: the owner's brief reaches the art director, and its words are the cards' words")

        # A card the contract REJECTS is rewritten, not thrown away. One over-long line used to
        # take the whole art direction with it — identity AND every card — and the reel was then
        # queued with no art direction at all.
        _long = json.loads(json.dumps(_ident()))
        _long["captions"]["hook"]["lines"] = ["दादाजी रोज़ सुबह 5 बजे Good Morning भेजते हैं..."]
        _n, _sent = {"n": 0}, []
        def _stub_retry(msgs, **kw):
            if "CAPTION CARDS" in msgs[-1]["content"]:
                return json.dumps(_ident()), "stub"
            _sent.append(msgs[-1]["content"])
            _n["n"] += 1
            return json.dumps(_long if _n["n"] == 1 else _ident()), "stub"
        globals()["chat"] = _stub_retry
        try:
            got = presentation("script", _shot)
            assert _n["n"] == 2, f"a rejected card must be rewritten, not fatal: {_n['n']} calls"
            assert got["captions"]["hook"]["lines"] == _ident()["captions"]["hook"]["lines"], \
                "the SECOND answer must be the one adopted"
            assert "REJECTED" in _sent[-1], "the rewrite must be told what was wrong"
            def _always_long(msgs, **kw):
                if "CAPTION CARDS" in msgs[-1]["content"]:
                    return json.dumps(_ident()), "stub"
                return json.dumps(_long), "stub"
            globals()["chat"] = _always_long
            try:
                presentation("script", _shot)
                raise AssertionError("a card that never fits must stop, not be written anyway")
            except RuntimeError as e:
                assert "art direction failed" in str(e), f"say what happened, got: {e}"
        finally:
            globals()["chat"] = _orig_chat2
        print("OK: a card the contract rejects is rewritten with the reason fed back, "
              "and the attempts are bounded")

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
        # the brand's domain is a fact: an invented or shortened one is rewritten, not shipped
        assert _fix_url("standardsunrise.org पर आएँ") == f"{BRAND_URL} पर आएँ", "domain kept"
        assert _fix_url("visit www.example.com/join today") == f"visit {BRAND_URL} today", "url"
        assert _fix_url("आपका कुछ मिनट उनका खज़ाना है") == "आपका कुछ मिनट उनका खज़ाना है", \
            "a card with no address must come back untouched"
        assert BRAND_URL in _wavelength(), "WAVELENGTH §8 and agents.BRAND_URL have drifted apart"
        # §5: the identity lock and the per-site shape are prose facts a future edit could delete
        # silently. The lock's own wording is asserted in _prompt_selfcheck; this is the drift
        # guard between the wavelength and the code that enforces it.
        assert "never trimmed off the end of a prompt" in _wavelength(), \
            "WAVELENGTH §5 no longer says the identity lock outranks the length trim"
        assert "Vids and Flow are both Veo 3.1" in _wavelength(), \
            "WAVELENGTH §5 no longer names the two sites that share the labelled brief"
        # §6: a down gateway is the agent's cue to LOOK, not the owner's. Prose drifts silently;
        # this keeps the one rule the owner had to say twice from being edited away.
        assert "the agent's job, not a ticket for the owner" in _wavelength(), \
            "WAVELENGTH §6 no longer says the by-eye fallback is performed by the agent"
        # §9: the folder contract is prose the build now ENFORCES (qc.check_reel_folder reads
        # publishing.FOLDER), so the two must not drift. If the section is edited out, the gate
        # keeps demanding files the wavelength no longer explains.
        assert "The folder is the deliverable" in _wavelength() \
            and "publishing.md" in _wavelength(), \
            "WAVELENGTH §9 no longer states the folder contract the gate enforces"
        print("OK: any web address on a card is rewritten to the foundation's own")

        # the publishing sheet's contract: a sheet that would go out must open on the reel's own
        # hook line and carry the foundation's real address — never an invented one.
        _sheet = {"youtube_title": "दादाजी की 4 बातें", "youtube_title_alt": "अकेले दादाजी",
                  "caption": f"अस्सी की उम्र में मैंने ये बातें सीखीं…\n\n{BRAND_URL}\n",
                  "youtube_description": "एक अकेले दादाजी की कहानी",
                  "tags": "a, b", "keywords": "a, b",
                  "hashtags": " ".join(f"#h{i}" for i in range(12))}
        _publishing_selfcheck(_sheet, "अस्सी की उम्र में मैंने ये बातें सीखीं…")
        for _bad in ({**_sheet, "caption": _sheet["caption"].replace("अस्सी", "पचास")},
                     {**_sheet, "caption": f"जुड़िये standardsunrise.org पर\n"},
                     {**_sheet, "hashtags": "#one #two"},
                     {**_sheet, "youtube_title": "  "}):
            try:
                _publishing_selfcheck(_bad, "अस्सी की उम्र में मैंने ये बातें सीखीं…")
            except AssertionError:
                continue
            raise AssertionError(f"publishing_sheet accepted a sheet it must refuse: {_bad}")
        print("OK: a publishing sheet must open on the reel's own hook and the real address")
        # a hook card split over two lines: the caption must open on the FIRST line (the one the
        # profile grid shows) — the check must not demand a join of the two that is not a line.
        _two = {"youtube_title": "दादाजी की धीमी चाल", "youtube_title_alt": "धीमी चाल",
                "caption": f"बच्चे दादाजी की धीमी चाल पर हँस रहे थे...\n\n{BRAND_URL}\n",
                "youtube_description": "एक धीमी चाल की कहानी",
                "tags": "a, b", "keywords": "a, b",
                "hashtags": " ".join(f"#h{i}" for i in range(12))}
        _publishing_selfcheck(_two, "बच्चे दादाजी की धीमी चाल पर हँस रहे थे...")
        for _bad in ({**_two, "caption": "फिर दीवार पर एक फ़ोटो दिखी।\n\n" + BRAND_URL},
                     {**_two, "caption": _two["caption"].replace("धीमी चाल", "तेज़ चाल")}):
            try:
                _publishing_selfcheck(_bad, "बच्चे दादाजी की धीमी चाल पर हँस रहे थे...")
            except AssertionError:
                continue
            raise AssertionError(f"publishing_sheet accepted a sheet it must refuse: {_bad}")
        print("OK: a two-line hook card opens the caption on its FIRST line")
        print("OK: presentation() rejects overflow, an empty card, an English-only hook, "
              "a photoreal identity, and an unknown font")

        # ---- THE SCRIPT'S OWN CONTRACT: written, judged, rewritten — before it costs anything ----
        # Prose rules drift silently, so the writer's rubric is asserted the way WAVELENGTH's is.
        assert _story().strip(), "STORY.md must exist: it is the writer's rubric and the critic's bar"
        assert "Banned openings" in _story(), "STORY.md no longer bans a preface as a hook"
        assert "Never the same reel twice" in _story(), "STORY.md lost the anti-repeat rule"
        assert "the word budget is arithmetic" in _story().lower(), \
            "STORY.md no longer states the word budget as a rule"
        # Read at CALL time (the _wavelength() contract): a rule the owner adds to the file lands on
        # the very next run, with no code change and no model swap able to lose it.
        import tempfile
        _probe = os.path.join(tempfile.mkdtemp(), "STORY.md")
        with open(_probe, "w") as f:
            f.write("# a rule added one second ago\n")
        _orig_md = globals()["_STORY_MD"]
        try:
            globals()["_STORY_MD"] = _probe
            assert "one second ago" in _system("ROLE", writing=True), \
                "STORY.md must be read at call time, not cached at import"
            assert "one second ago" not in _system("ROLE"), \
                "the caption judge must not carry the writing rules — its prompt is long enough"
        finally:
            globals()["_STORY_MD"] = _orig_md

        # A whole script is whole; a script cut off mid-table-row is NOT — and that is the defect
        # that shipped: dadi-ki-bucket-list's script.md ends on a bare "| 8 | **" and agent 2 filmed
        # the fragment. A missing MORAL is the same failure one step earlier.
        _WHOLE = ("MEDIUM: a 3-D animated film\nLOGLINE: A son wants his father's voice back.\n"
                  "VISUAL STYLE: soft stylized 3-D animation, amber lamplight.\n"
                  "BEATS:\n"
                  "| 1 | He holds the dead phone | क्या तुम्हें यह आवाज़ याद है? | 8 |\n"
                  "| 2 | He plays the recording | *silent* | 5 |\n"
                  "MORAL: पिता की आवाज़ कभी नहीं जाती\n"
                  "CTA: standardsunrisefoundation.org\n")
        _FIXED = _WHOLE.replace("क्या तुम्हें यह आवाज़ याद है?", "यह आवाज़ मेरे पास अब भी है")
        assert _script_complete(_WHOLE) == [], "a whole script must pass"
        assert _script_complete(_FIXED) == [], "the rewrite must be a whole script too"
        assert _script_complete(_WHOLE + "| 8 | **"), "a script cut off mid-table must be caught"
        assert _script_complete("LOGLINE: x\nVISUAL STYLE: y\nBEATS: z"), \
            "a script with no MORAL is incomplete"
        assert _script_complete(""), "an empty answer is not a script"
        # the finding -> lesson tag, pure (nothing is filed by this assertion)
        assert _TAG.search("[budget] 24 words in a 7-second line").group(1) == "budget", \
            "a tagged finding must name its rule"
        assert not _TAG.search("the hook is a preface"), "an untagged finding files no lesson"
        # A critic answer cut off before its closing brace still carries its findings — they are
        # complete strings long before the end, and throwing them away as an outage loses the one
        # thing the call was worth.
        assert _salvage_problems('{"problems": ["[hook] a preface — open on the object", '
                                 '"[shape] the beats are swappable"], "script": "MEDIUM: 3-D') == \
            ["[hook] a preface — open on the object", "[shape] the beats are swappable"], \
            "a truncated critique must still teach the brain what it found"
        assert _salvage_problems("no json at all") == [], "nothing is invented from prose"

        _orig_chat3, _orig_file = chat, _file_script_lessons
        _filed = []

        def _critic_says(problems, script):
            """A stub `chat` that answers as the critic when the prompt is the critic's."""
            def _stub(messages, **k):
                if "story editor" in messages[0]["content"]:
                    return json.dumps({"problems": problems, "script": script}), "critic"
                return _WHOLE, "writer"
            return _stub
        try:
            globals()["_file_script_lessons"] = lambda p: _filed.append(list(p))

            # the critic's rewrite is ADOPTED, its findings are recorded, and the rounds are capped —
            # a stub that always finds something must not loop forever
            globals()["chat"] = _critic_says(["[hook] opens on a preface"], _FIXED)
            out = write_script("a test idea")
            assert out == _FIXED, "the critic's rewrite must be what gets filmed"
            assert LAST_CRITIQUE["rounds"] == SCRIPT_ROUNDS, \
                f"the loop must stop at SCRIPT_ROUNDS, ran {LAST_CRITIQUE['rounds']}"
            assert len(LAST_CRITIQUE["problems"]) == SCRIPT_ROUNDS, \
                "every pass's findings are recorded, not just the last"
            assert _filed and _filed[0][0] == "[hook] opens on a preface", \
                "a tagged finding must reach the brain as a candidate lesson"

            # a critic that is DOWN costs nothing: the draft ships and the outage is recorded
            def _down(messages, **k):
                if "story editor" in messages[0]["content"]:
                    raise RuntimeError("all LLM gateways failed: stub")
                return _WHOLE, "writer"
            globals()["chat"] = _down
            assert write_script("a test idea") == _WHOLE, "a critic outage must not cost the draft"
            assert LAST_CRITIQUE["could_not_run"], \
                "an outage is recorded — a check that could not run is not a pass"
            assert not _filed[-1], "nothing is filed from a critique that never happened"

            # a critic that returns HALF a document is refused: we never end up with less
            globals()["chat"] = _critic_says(["[shape] a list, not a story"], _WHOLE + "| 8 | **")
            assert write_script("a test idea") == _WHOLE, \
                "a truncated rewrite must never replace a whole draft"

            # truncated WRITING is finished by one continuation call, and only if it helps
            _cut = _WHOLE.split("MORAL:")[0]
            _writes = []

            def _cut_then_rest(messages, **k):
                if "story editor" in messages[0]["content"]:
                    return json.dumps({"problems": [], "script": ""}), "critic"
                _writes.append(messages[-1]["content"])
                if len(_writes) == 1:
                    return _cut, "writer"
                return "MORAL: पिता की आवाज़ कभी नहीं जाती\nCTA: standardsunrisefoundation.org", "w"
            globals()["chat"] = _cut_then_rest
            out = write_script("a test idea")
            assert "MORAL" in out and not _script_complete(out), \
                "an answer cut off mid-document must be finished, not shipped"
            assert len(_writes) == 2 and "cut off" in _writes[1], \
                "exactly one continuation call, told what was missing — never a barrage"
        finally:
            globals()["chat"] = _orig_chat3
            globals()["_file_script_lessons"] = _orig_file
        print("OK: the script is written, judged against STORY.md, rewritten, and finished — and a "
              "critic that is down costs the reel nothing")
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
