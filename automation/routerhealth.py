"""
routerhealth.py — remember which gateway is WALLED, so a run stops asking a dead route.

A free router answers 429 "Too Many Requests" most days. Every call site here retries a gateway
before moving on (that is deliberate — waiting out a busy tier beats abandoning it), but nothing
recorded the outcome, so the NEXT call re-discovered the same wall: three retries and ~4.5s of
sleep, per call, for a route that had already said no. Measured on the craft gate, the frame
review alone makes dozens of vision calls, so one walled gateway cost minutes of a run spent
knocking on a door it had already been refused.

This is the memory. A route that fails is skipped until its cooldown expires; a route that
answers clears itself immediately. Nothing is EVER permanent — a route skipped forever is a
route this project forgets how to use, and a free tier resets.

Two shapes of failure, because they mean different things:
  * a WALL (429 / quota / auth) — the tier is done for now. Wait it out, longer.
  * a BLIP (timeout, reset, empty answer) — a bad second, not a bad route. Short cooldown.

ponytail: in-process dict, no persistence, cooldowns read from the environment rather than
re-reading .env (the callers already load it). A run is one process, and a cooldown that survived
a restart would outlive the 429 it was recorded for; if a wrapper ever drives several processes,
key this on state.db instead.
"""

import os
import time

import os
import time

WALL = float(os.environ.get("LLM_COOLDOWN", "120"))       # 429/quota/auth: done for now
BLIP = float(os.environ.get("LLM_BLIP_COOLDOWN", "30"))   # a timeout or a bad second

# What separates a wall from a blip. Quota wording varies by gateway ("insufficient credits",
# "rate limit", "exceeded your current quota"), so match words, not just the status code.
_WALL_WORDS = ("429", "too many requests", "rate limit", "quota", "insufficient",
               "401", "403", "unauthorized", "forbidden")

_WALLED = {}                                    # key -> (until_ts, why)


def _get_st():
    try:
        from state import State
        return State()
    except Exception:
        return None


def cooling(key: str):
    """(until_ts, why) when `key` is walled RIGHT NOW, else (0.0, ""). Expiry is checked on read,
    so a spent cooldown never needs cleaning up."""
    now = time.time()
    if key in _WALLED:
        until, why = _WALLED[key]
        return (until, why) if until > now else (0.0, "")

    # If key not in in-memory dict, check persistent state.db (cross-process memory)
    st = _get_st()
    if st:
        try:
            cd = st.cooldown_left("llm:" + key, now=now)
            if cd > 0:
                _WALLED[key] = (now + cd, "persisted cooling in state.db")
                return now + cd, "persisted cooling in state.db"
        finally:
            st.close()

    return 0.0, ""


def note_failure(key: str, err) -> float:
    """Record a failure against `key` and return the cooldown applied, in seconds."""
    secs = WALL if any(w in str(err).lower() for w in _WALL_WORDS) else BLIP
    now = time.time()
    _WALLED[key] = (now + secs, str(err)[:90])

    st = _get_st()
    if st:
        try:
            st.record_lesson("llm:" + key, "cooldown", detail=str(err)[:90], cooldown_s=secs)
        finally:
            st.close()

    return secs


def note_success(key: str):
    """A route that answered is a route that works — clear its wall at once, or a recovered
    gateway stays skipped for the rest of its cooldown and the run quietly loses a route."""
    _WALLED.pop(key, None)
    st = _get_st()
    if st:
        try:
            st.db.execute("UPDATE lessons SET cooldown_until=0 WHERE site=?", ("llm:" + key,))
            st.db.commit()
        finally:
            st.close()


def walled() -> dict:
    """Every key cooling right now, with its reason — for the error a caller raises when it
    has nothing left. Without this the operator sees 'all gateways failed' and no hint that
    half of them were never tried."""
    return {k: w for k, w in list(_WALLED.items()) if cooling(k)[0]}


def clear():
    _WALLED.clear()
    st = _get_st()
    if st:
        try:
            st.db.execute("UPDATE lessons SET cooldown_until=0 WHERE site LIKE 'llm:%'")
            st.db.commit()
        finally:
            st.close()


if __name__ == "__main__":
    clear()
    assert cooling("a") == (0.0, ""), "an unseen route is not cooling"
    assert note_failure("a", "HTTP Error 429: Too Many Requests") == WALL, "429 is a wall"
    assert cooling("a")[0] > time.time(), "a walled route must read as cooling"
    assert "429" in cooling("a")[1], "the reason must survive, so a failure can explain itself"
    assert note_failure("b", "timed out") == BLIP, "a timeout is a blip, not a wall"
    assert cooling("b")[0] < cooling("a")[0], "a blip must expire before a wall does"
    note_success("a")
    assert cooling("a") == (0.0, ""), "a route that answers must clear itself at once"
    # a spent cooldown must release the route — faked, rather than slept through
    note_failure("c", "insufficient credits")
    _WALLED["c"] = (time.time() - 1, _WALLED["c"][1])
    assert cooling("c") == (0.0, ""), "a spent cooldown must not keep a route skipped"
    assert "c" not in walled(), "…and must drop out of the report too"
    clear()
    print("OK: router health — a wall cools a route, a blip cools it briefly, success clears it, "
          "and nothing is ever skipped forever")
