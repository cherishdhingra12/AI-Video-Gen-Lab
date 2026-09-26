"""
learn.py — the anti-ban brain, sitting on top of state.lessons.

The rule (user's own): bans come from repeating the same mistake. So:
  before_action()  consult past lessons for (site,account); if it's cooling down,
                   say WAIT with seconds left — step back, don't attack now.
  hit_wall()       a captcha / rate-limit / block just happened. Record it and set
                   an ESCALATING cooldown (each repeat waits longer). Come back later.
  note_success()   clears the escalation memory for that pair (the wall is gone).

The scheduler asks before_action() which pairs are attackable; the adapter calls
hit_wall()/note_success() as it learns from each attempt. Nothing here reaches the
network — it's pure policy over the durable lessons table, so it's cheap and testable.
"""

import time

from state import State, backoff_seconds

# how a wall maps to an escalation floor (multiplied by 2^prior_hits in backoff)
_WALL_BASE = {
    "captcha": 600.0,       # 10 min, doubling
    "rate_limit": 900.0,    # 15 min
    "block": 3600.0,        # 1 h — serious, wait long
    "selector": 120.0,      # UI probably changed; short wait, adapter may self-heal
}


class Brain:
    def __init__(self, st: State = None):
        self.st = st or State()

    def _prior_hits(self, site: str, account: str, kind: str, window_s=24 * 3600) -> int:
        cutoff = time.time() - window_s
        rows = self.st.recent_lessons(site, account, kind=kind, limit=20)
        return sum(1 for r in rows if r["created"] >= cutoff)

    def before_action(self, site: str, account: str) -> dict:
        """Ask permission to act on (site,account) now.
        {'go': bool, 'wait_s': float, 'why': str}."""
        wait = self.st.cooldown_left(site, account)
        if wait > 0:
            recent = self.st.recent_lessons(site, account, limit=1)
            why = recent[0]["kind"] if recent else "cooldown"
            return {"go": False, "wait_s": wait, "why": f"cooling down after {why}"}
        return {"go": True, "wait_s": 0.0, "why": "clear"}

    def hit_wall(self, site: str, account: str, kind: str, step: str = None,
                 detail: str = None) -> float:
        """Record the wall and cool the pair down, escalating on repeats.
        Returns the cooldown seconds applied (step back, come back after)."""
        base = _WALL_BASE.get(kind, 300.0)
        prior = self._prior_hits(site, account, kind)
        cd = backoff_seconds(prior, base=base)
        self.st.record_lesson(site, kind, account=account, step=step,
                              detail=detail, cooldown_s=cd)
        return cd

    def note_success(self, site: str, account: str):
        """A clean run — drop any lingering cooldown so we return at full speed."""
        self.st.record_lesson(site, "success", account=account, cooldown_s=0)

    def pick(self, want_site: str = None) -> tuple:
        """Scheduler helper: best (site,account) that has credit AND isn't cooling.
        want_site is a soft PREFERENCE (tried first), not a filter — a job whose last
        site is now cooling must still reroute, or the whole waterfall stalls on it.
        Returns (site,account) or None."""
        cands = self.st.available()
        # most remaining credit first = spread load, delay hitting any one wall;
        # then float want_site to the front (stable sort keeps credit order within groups)
        cands.sort(key=lambda c: c["remaining"], reverse=True)
        if want_site:
            cands.sort(key=lambda c: c["site"] != want_site)
        for c in cands:
            if self.before_action(c["site"], c["account"])["go"]:
                return (c["site"], c["account"])
        return None


if __name__ == "__main__":
    import os, tempfile
    st = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    br = Brain(st)
    st.set_credits("kling", "a1", remaining=6)
    st.set_credits("kling", "a2", remaining=3)

    # clear pair -> go
    assert br.before_action("kling", "a1")["go"], "clean pair should go"

    # hit a captcha -> cools down, and a repeat escalates to a longer wait
    cd1 = br.hit_wall("kling", "a1", "captcha")
    assert not br.before_action("kling", "a1")["go"], "must step back while cooling"
    cd2 = br.hit_wall("kling", "a1", "captcha")
    assert cd2 > cd1, f"repeat must wait longer: {cd1} -> {cd2}"

    # scheduler routes around the cooling pair to the healthy one
    picked = br.pick("kling")
    assert picked == ("kling", "a2"), f"should route to healthy account, got {picked}"

    # nothing healthy on the wanted site -> None (everything either dry or cooling)
    st.spend_credit("kling", "a2", 3)
    assert br.pick("kling") is None, "no credit + cooling -> nothing to pick"

    # want_site is a PREFERENCE: when it's cooling/dry, reroute to a healthy other site
    st.set_credits("pika", "a1", remaining=5)
    assert br.pick("kling") == ("pika", "a1"), "must reroute off a cooling want_site"
    print(f"OK: consult->go, wall cools+escalates ({cd1:.0f}->{cd2:.0f}s), "
          f"scheduler routes around cooling accounts and reroutes off a dead want_site")
