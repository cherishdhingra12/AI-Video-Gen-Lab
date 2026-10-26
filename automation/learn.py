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

# The site waterfall (owner's call, 2026-10-10): Google Vids FIRST — it draws one consistent
# character and carries the native in-scene Hindi voice — then Flow, then everything else.
# Sites not listed share last place, ordered among themselves by remaining credit. One reel is
# still ONE source: this only decides which site a reel STARTS on (see runner.process_job).
SITE_ORDER = ["googlevids", "flow"]


def _order(site: str) -> int:
    return SITE_ORDER.index(site) if site in SITE_ORDER else len(SITE_ORDER)


# how a wall maps to an escalation floor (multiplied by 2^prior_hits in backoff)
_WALL_BASE = {
    "captcha": 600.0,       # 10 min, doubling
    "rate_limit": 900.0,    # 15 min
    "block": 600.0,         # 10 min, doubling — owner ruling 2026-10-16, was 1 h. `block` is
                            # raised for two unlike things: a structural fact about the account
                            # (geo-blocked, paywalled, out of scope) and a transient failure (a
                            # download HTTP 500, a render that timed out). An hour suited the first
                            # and was far too long for the second — one slow Vids render cost this
                            # reel an hour for nothing. Ten minutes retries the transient case
                            # promptly, and because the floor doubles per prior hit a genuinely
                            # permanent wall pushes itself back out (10 -> 20 -> 40 min) without
                            # anyone having to classify the 22 call sites by hand.
    "selector": 120.0,      # UI probably changed; short wait, adapter may self-heal
    "credits": 0.0,         # OUT, not cooling. Being empty is not a mistake to step back from,
                            # and a cooldown here would just send the scheduler back in 15
                            # minutes to the same empty site. The pair is marked dry in the
                            # ledger instead (state.mark_dry) and returns at its REFILL time.
}

DAILY, MONTHLY = "daily", "monthly"


def refill_at(period: str, now: float = None) -> float:
    """When a dry site comes back — the wall says so in words, this turns it into a time.

    '' = no known refill (a one-time signup grant, a paywall): stays 0, which `available()`
    reads as "no refill time known" — dry until a person says otherwise. Local midnight is the
    honest approximation for the daily and monthly buckets: Google resets on Pacific time, Krea
    on its own, and being a few hours EARLY costs one retry while being late costs a run.
    """
    t = time.localtime(now or time.time())
    if period == DAILY:
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    if period == MONTHLY:
        y, m = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
        return time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
    return 0.0


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

    def wait_for_bid(self, site: str) -> bool:
        """Should a reel WAIT for this site rather than fail the whole reel over to a lower tier?

        This is the difference the bid branch of runner.process_job was missing — a soft
        `want_site` fell straight through the moment the primary cooled, so ONE `selector` wall
        (120s cooldown) moved a 12-credit Google Vids reel onto vibes: silent, 464x832, and the
        reel came back with no narration and its dialogue burned in as garbled subtitles.

        True only when the site is not usable RIGHT NOW but is still worth waiting for. A site
        that is ready returns False (go straight to it); so do the two real dead ends — dry, and
        hard-`block`ed (_WALL_BASE: 10 min and escalating, which means "go elsewhere", not "come
        back shortly"). No bid at all returns False: the waterfall picks freely."""
        if not site:
            return False
        if self.pick(want_site=site, strict=True):
            return False                       # ready now — nothing to wait for
        if not self.st.site_has_credit(site):
            return False                       # dry — a real dead end
        recent = self.st.recent_lessons(site, limit=1)
        if recent and recent[0]["kind"] in ("block", "credits"):
            return False                       # hard-blocked, or the site just said it is empty
        return True                            # funded but cooling: worth the wait

    def pick(self, want_site: str = None, strict: bool = False, need: int = 0,
             cost_of=None) -> tuple:
        """Scheduler helper: best (site,account) that has credit AND isn't cooling.
        want_site is a soft PREFERENCE (tried first), not a filter — a job whose last
        site is now cooling must still reroute, or the whole waterfall stalls on it.

        strict=True makes want_site a HARD filter: the reel is already being shot on that
        source, so nothing else may add a scene to it (one reel, one look). None comes back
        when it is cooling or dry, and the caller waits rather than mixing the reel — a
        stall costs a delay, a mix costs the consistency the reel was built for.
        Returns (site,account) or None.

        `need` is how many clips are still to be rendered for THIS reel and `cost_of(site)`
        what one clip costs there: a site is then ranked by the clips it can AFFORD, not by its
        raw balance. 1000 leftover credits are worthless for a 4-clip reel if one clip costs
        134, and the reel that starts on a site with 2 left strands after clip 1 — locked to
        that source (one reel, one source) and waiting for a refill to finish a half-made film.
        Affordability outranks the house waterfall ONLY when it decides whether the reel can be
        finished at all; among affordable sites the quality order still wins, and when nobody
        can afford the whole reel the order is exactly what it was.

        THE ACCOUNT WATERFALL (owner, 2026-10-26): a site with several accounts must spend
        the fullest one first — a1 drains to 0 before a2 starts, a2 before a3 — matching
        `Vids1 -> Vids2 -> Vids3 -> Flow1 -> Flow2 -> Flow3` on exhaustion. The cursor is
        remaining credit itself: state.rotation orders by credit left, so there is no separate
        "where were we" state to fall out of sync with the ledger. (This replaced the older
        used_at cycle, which bounced back to a funded a1.)"""
        def afford(c) -> int:
            cost = max(1, int(getattr(cost_of(c["site"]), "credit_cost", 1) if cost_of else 1))
            return c["remaining"] // cost

        cands = self.st.available()
        # The house waterfall decides the ORDER of SITES: Vids before Flow before the rest, and
        # within a tier the most remaining credit first (spread load, delay hitting any one
        # wall). This is a QUALITY order, not a credit order — a small Vids bucket must not lose
        # to a fat krea one.
        # Affordability sits ABOVE all of it (see `need`): it is the one thing that decides whether
        # the reel can be finished at all, and a source that strands it is no source.
        # …and WITHIN one site the accounts run as a waterfall (the rotation, below), the
        # fullest first. The rotation rank is comparable only inside a site, so it sits BELOW
        # the site order in the key and the credit tiebreak sits below it — a difference of
        # site decides first.
        rank = {}
        for site in {c["site"] for c in cands}:
            here = [c["account"] for c in cands if c["site"] == site]
            for i, a in enumerate(self.st.rotation(site, here)):
                rank[(site, a)] = i
        cands.sort(key=lambda c: (0 if afford(c) >= need else 1,
                                  0 if want_site and c["site"] == want_site else 1,
                                  _order(c["site"]), rank.get((c["site"], c["account"]), 99),
                                  -afford(c)))
        if strict:
            cands = [c for c in cands if c["site"] == want_site]
        for c in cands:
            if self.before_action(c["site"], c["account"])["go"]:
                return (c["site"], c["account"])
        return None


if __name__ == "__main__":
    import os, tempfile, types
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
    # strict=True makes want_site a LOCK, not a preference: a reel already shooting on kling
    # must WAIT for kling rather than let pika add a scene with a different look.
    assert br.pick("kling", strict=True) is None, \
        "a locked reel must wait for its own source, never fill in from another site"
    st.spend_credit("pika", "a1", 5)                  # pika is dry again
    st.db.execute("UPDATE lessons SET cooldown_until=0")   # kling's cooldown lapses
    st.db.commit()
    st.set_credits("kling", "a1", remaining=2)
    assert br.pick("kling", strict=True) == ("kling", "a1"), \
        "the lock lifts the moment its own source is healthy again"

    # The house waterfall (owner, 2026-10-10): Vids FIRST, then Flow, then everyone else
    # (credit-sorted among themselves). This is a QUALITY order, not a credit order — a thin
    # Vids bucket must outrank a fat krea one, or the reel stops being one consistent world.
    pst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    pbr = Brain(pst)
    pst.set_credits("googlevids", "g1", remaining=1)
    pst.set_credits("flow", "f1", remaining=9)
    pst.set_credits("krea", "k1", remaining=50)
    pst.set_credits("kling", "a1", remaining=40)
    assert pbr.pick() == ("googlevids", "g1"), "Vids wins the waterfall even on 1 credit"
    pst.spend_credit("googlevids", "g1", 1)                 # Vids dry
    assert pbr.pick() == ("flow", "f1"), "Flow is next after Vids"
    pst.spend_credit("flow", "f1", 9)                       # Flow dry too
    assert pbr.pick() == ("krea", "k1"), "beyond Vids/Flow the most remaining credit wins"
    print(f"OK: consult->go, wall cools+escalates ({cd1:.0f}->{cd2:.0f}s), "
          f"scheduler routes around cooling accounts and reroutes off a dead want_site — "
          f"but a STRICT want_site (one reel, one look) waits instead of rerouting; "
          f"the waterfall is Vids->Flow->rest, not credit-ordered")

    # THE CLIPS A SITE CAN AFFORD (owner fix, 2026-10-14). Raw balance is the wrong ranking: a
    # reel that opens on a site with 2 clips left strands after clip 1 — locked to that source
    # (one reel, one source), waiting for a refill to finish a half-made film, with every clip
    # already rendered riding on it.
    ast = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    abr = Brain(ast)
    ast.set_credits("googlevids", "g1", remaining=2)     # 1 credit/clip
    ast.set_credits("vibes", "v1", remaining=100)        # 25 credits/clip (measured)
    costs = types.SimpleNamespace(googlevids=types.SimpleNamespace(credit_cost=1),
                                  vibes=types.SimpleNamespace(credit_cost=25))
    cost_of = lambda site: costs.__dict__[site]          # noqa: E731
    assert abr.pick(want_site="googlevids", need=2, cost_of=cost_of) == ("googlevids", "g1"), \
        "a site that CAN finish the reel still wins on the house order"
    assert abr.pick(want_site="googlevids", need=3, cost_of=cost_of) == ("vibes", "v1"), \
        "a reel Vids cannot finish must open where it can — 2 clips of credit is not a source"
    assert abr.pick(need=99, cost_of=cost_of) == ("googlevids", "g1"), \
        "when NO site can afford the whole reel the order is what it always was, never nothing"

    # A DRY SITE COMES BACK AT ITS OWN REFILL TIME, not after a cooldown. The site names the
    # period in words; this turns it into a time the ledger can hold.
    now = time.time()
    d = refill_at(DAILY, now)
    assert 0 < d - now <= 24 * 3600 + 60, f"a daily refill is within a day, got {d - now}"
    assert time.localtime(d)[3:6] == (0, 0, 0), "a daily bucket refills at local midnight"
    assert time.localtime(d)[2] >= time.localtime(now)[2], "and on the NEXT day"
    m = refill_at(MONTHLY, now)
    assert time.localtime(m)[2] == 1 and 0 < m - now <= 32 * 24 * 3600, "a monthly one on the 1st"
    dec = time.mktime((2026, 12, 20, 10, 0, 0, 0, 0, -1))
    assert time.localtime(refill_at(MONTHLY, dec))[:2] == (2027, 1), \
        "December must roll into January, not month 13"
    assert refill_at("") == 0.0, \
        "a site that named no period stays UNKNOWN — 0 is never a guessed date"
    print("OK: a site is ranked by the clips it can afford, and a dry one returns at the refill "
          "time it named (or stays unknown when it named none)")

    # THE ACCOUNT WATERFALL (owner, 2026-10-26): three Google accounts on 'googlevids', used in
    # order of remaining credit — the fullest is always picked, so a1 drains before a2 before a3.
    rst = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    rbr = Brain(rst)
    # a1 funded; a2, a3 dry (refill far in the future) -> a1 alone carries the reel
    rst.set_credits("googlevids", "a1", remaining=50)
    rst.set_credits("googlevids", "a2", remaining=0, resets_at=time.time() + 9999)
    rst.set_credits("googlevids", "a3", remaining=0, resets_at=time.time() + 9999)
    assert rbr.pick(want_site="googlevids") == ("googlevids", "a1"), \
        "only a1 is funded -> a1 is picked"
    # a1 drains to 1 while a2 refills to 50: the FULLEST (a2) goes, not a1
    rst.set_credits("googlevids", "a1", remaining=1)
    rst.set_credits("googlevids", "a2", remaining=50)
    rst.set_credits("googlevids", "a3", remaining=49)
    assert rbr.pick(want_site="googlevids") == ("googlevids", "a2"), \
        "the fullest account goes — a2 (50) over a3 (49) over a1 (1)"
    # all three dry -> None, so the scheduler cascades to the NEXT site (Flow)
    rst.set_credits("googlevids", "a1", remaining=0, resets_at=time.time() + 9999)
    rst.set_credits("googlevids", "a2", remaining=0, resets_at=time.time() + 9999)
    rst.set_credits("googlevids", "a3", remaining=0, resets_at=time.time() + 9999)
    assert rbr.pick(want_site="googlevids") is None, \
        "when every Vids account is dry the waterfall returns None, cascading to Flow"
    print("OK: the accounts run in a WATERFALL (fullest first — a1 drips to 0 before a2/a3), "
          "and a dry site falls through (Vids1->Vids2->Vids3 then Flow)")

    # THE COUNT MUST BE HONEST (the owner's priority, 2026-10-22). Vids shows no balance, so
    # the count is derived: the bucket size is LEARNED from the refusal, and every generation
    # is counted off the top.
    pst2 = State(os.path.join(tempfile.mkdtemp(), "t.db"))
    pst2.set_pool("googlevids", "a1", 50, email="one@example.com")
    assert pst2.account_email("googlevids", "a1") == "one@example.com", "the email is recorded"
    pst2.set_credits("googlevids", "a1", remaining=50, source="manual")
    for _ in range(7):
        pst2.spend_credit("googlevids", "a1", 1)
    assert pst2.db.execute("SELECT remaining FROM credits WHERE site='googlevids'").fetchone()[0] == 43, \
        "each generation must come off the count"
    assert pst2.db.execute("SELECT used FROM credits WHERE site='googlevids'").fetchone()[0] == 7, \
        "and be counted, so the pool size can be learned from a refusal"
    pst2.mark_dry("googlevids", "a1", refill_at=time.time() + 9999)
    assert pst2.db.execute("SELECT used FROM credits WHERE site='googlevids'").fetchone()[0] == 0, \
        "a spent bucket restarts its counter — the next refusal teaches the next period"
    assert pst2.db.execute("SELECT pool FROM credits WHERE site='googlevids'").fetchone()[0] == 50, \
        "a refusal must not shrink a pool below the generations that were actually made"
    # a refilled bucket comes back at the pool's size, not at zero — but only when a generation
    # actually goes through, which is the proof. A clock running out is NOT a refill.
    pst2.mark_dry("googlevids", "a1", refill_at=time.time() - 1)
    assert pst2.db.execute("SELECT remaining FROM credits WHERE site='googlevids'").fetchone()[0] == 0, \
        "a passing refill time must NOT hand out credits on its own — that would be a fiction"
    pst2.spend_credit("googlevids", "a1", 1)
    assert pst2.db.execute("SELECT remaining FROM credits WHERE site='googlevids'").fetchone()[0] == 49, \
        "a generation that goes through on a dry account IS the proof the bucket came back"
    print("OK: every generation is counted off the balance, a refusal teaches the pool size, "
          "and a bucket only refills when a generation proves it")
