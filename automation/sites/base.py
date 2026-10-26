"""
base.py — the contract every site adapter honors. The runner talks only to this,
so a new site is a new small file and a UI redesign touches only one adapter.

An adapter drives ONE generation on an already-logged-in browser Session:

    submit(session, prompt)  -> paste the prompt, click generate. raise Wall(...) if
                                a captcha / rate-limit / login blocks the way.
    poll(session)            -> look (selectors first, vision if unsure) and return
                                one of STATES. The runner loops on this.
    download(session, dest)  -> save the finished clip to dest, return the path.

Adapters never touch state/learn directly — they just raise Wall so the runner can
record the lesson and cool the site down. Keeps adapters tiny and side-effect-free.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vision import read_state, find_control, read_credits as _credits_from_png, STATES  # noqa: E402


class Wall(Exception):
    """A blocker we should step back from. kind in captcha|rate_limit|login|block|credits.

    `refill` belongs to kind='credits' and is the period the site itself named — 'daily',
    'monthly', or '' when it named none (a one-time grant, a paywall). Being OUT is not the
    same as being rate-limited: the runner marks the pair dry in the ledger with that refill
    time instead of cooling it for 15 minutes and coming back to an empty site."""
    def __init__(self, kind: str, detail: str = "", refill: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.refill = kind, detail, refill


class SiteAdapter:
    name = "base"
    home = ""                      # entry URL
    credit_cost = 1               # credits one generation spends
    prompt_sel = "textarea"       # overridden per site
    generate_sel = "button"
    download_sel = "a[download]"

    def submit(self, session, prompt: str):
        session.goto(self.home)
        self._guard(session)                       # bail early if walled
        session.human_type(self.prompt_sel, prompt)
        session.human_click(self.generate_sel)

    def poll(self, session) -> str:
        """Selector-first, vision-fallback. Returns a STATES value."""
        if session.has(self.download_sel, timeout=1500):
            return "done"
        return read_state(session.screenshot())["state"]

    def download(self, session, dest: str) -> str:
        return session.wait_download(self.download_sel, dest)

    # ---- live balance: read THIS account's credits off the page we're already on, so the
    # scheduler and brain.report work from observed numbers instead of hand-entered ones.
    # Default = one vision call (works on any site, nothing per-site to drift); an adapter
    # with a stable balance selector can override it. None = 'not clear' and the caller keeps
    # the last known number — an unknown balance never overwrites a known one. ----
    def read_credits(self, session):
        try:
            return _credits_from_png(session.screenshot())
        except Exception:
            return None

    # ---- WHICH ACCOUNT is this session? ------------------------------------------------
    # A site with several accounts needs the runner to know which one it is holding before it
    # spends a generation, because the failure is invisible otherwise: a cookie refresh that
    # hands account 2 the session of account 1, so a2 quietly spends a1's limit and the
    # per-account credit counts are wrong in a way no report would reveal.
    #
    # The site SAYS it, if you ask in the right dialect. Google's account avatar carries the
    # address in its aria-label — live on vids.google.com, one element, exactly:
    #   "Google Account: cherish dhingra  (cherishdhingra1234@gmail.com)"
    # so the identity is read from the page, not guessed from a file name. Returns '' when the
    # page says nothing, and '' means UNKNOWN — never a guess (see state.account_matches).
    _ACCOUNT_JS = """() => {
        const els = [...document.querySelectorAll('[aria-label*="Account" i],[aria-label*="account" i],'
            + '[data-email],[data-account-email]')];
        for (const e of els) {
            const t = (e.getAttribute('aria-label') || e.getAttribute('data-email')
                || e.getAttribute('data-account-email') || '');
            const m = t.match(/[\\w.+-]+@[\\w-]+\\.[\\w.]+/);
            if (m) return m[0];
        }
        return '';
    }"""

    def read_account(self, session) -> str:
        """The email address this session is signed in as, or '' when the page does not say."""
        try:
            return (session.page.evaluate(self._ACCOUNT_JS) or "").strip()
        except Exception:
            return ""

    def _guard(self, session):
        """Vision check for a wall before we spend effort. Raise Wall if blocked."""
        st = read_state(session.screenshot())
        if st["state"] in ("captcha", "rate_limited", "login"):
            kind = {"rate_limited": "rate_limit"}.get(st["state"], st["state"])
            raise Wall(kind, st["reason"])

    # ---- self-heal: when the saved selector misses because the UI drifted, use vision to find
    # the control NOW and keep running. Vision corrects the stale map; it never takes over. -------
    _CANDIDATES_JS = (
        "() => [...document.querySelectorAll('button,[role=button],a,[contenteditable],"
        "input,textarea,[tabindex],[role=menuitem],[role=option]')].map(e=>{"
        "const r=e.getBoundingClientRect();return {"
        "t:(e.innerText||e.getAttribute('aria-label')||e.placeholder||'').replace(/\\s+/g,' ').trim(),"
        "x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),"
        "w:Math.round(r.width),h:Math.round(r.height)};})"
        ".filter(o=>o.w>8&&o.h>8&&o.x>=0&&o.y>=0&&o.w<1400&&o.h<900)")

    def _candidates(self, session) -> list:
        try:
            raw = session.page.evaluate(self._CANDIDATES_JS)
        except Exception:
            return []
        return [{"i": i, "text": o["t"], "x": o["x"], "y": o["y"], "w": o["w"], "h": o["h"]}
                for i, o in enumerate(raw)]

    def heal_click(self, session, what: str):
        """A scripted click just missed (UI drift). Vision-locate the control and click it so
        the run continues on its own. Returns the {'x','y'} clicked, or None. Records the heal so
        the brain learns the drift (fail-open; the adapter stays otherwise side-effect-free)."""
        hit = find_control(session.screenshot(), self._candidates(session), what)
        if not hit:
            return None
        try:
            session.page.mouse.click(hit["x"], hit["y"])
        except Exception:
            return None
        print(f"[self-heal] {self.name}: '{what}' relocated by vision -> ({hit['x']},{hit['y']})")
        self._record_heal(what)
        return hit

    def click_or_heal(self, session, fast_fn, what: str) -> bool:
        """Try the fast scripted click; if it misses, self-heal via vision. Never raises on drift."""
        try:
            if fast_fn():
                return True
        except Exception:
            pass
        return bool(self.heal_click(session, what))

    def _record_heal(self, what: str):
        """Tell the brain a UI drift was healed (so recurring drift surfaces as a candidate rule).
        A 'selector' lesson with NO cooldown — we healed, we don't back off."""
        try:
            from state import State
            st = State()
            st.record_lesson(self.name, kind="selector", detail=f"healed via vision: {what}", cooldown_s=0)
            st.close()
        except Exception:
            pass


# ---- offline self-check: prove click_or_heal falls back to a vision heal and clicks the coords ---
if __name__ == "__main__":
    class _Pg:
        def __init__(self): self.clicked = None
        def evaluate(self, js): return []
        class _M:
            def __init__(s, pg): s.pg = pg
            def click(s, x, y): s.pg.clicked = (x, y)
        @property
        def mouse(self): return _Pg._M(self)

    class _Sess:
        def __init__(self): self.page = _Pg()
        def screenshot(self): return b"\x89PNG\r\n\x1a\n"

    ad = SiteAdapter()
    ad._record_heal = lambda what: None            # don't touch the real brain in the self-check
    _orig = find_control
    try:
        globals()["find_control"] = lambda png, cands, what: {"x": 321, "y": 123}
        s = _Sess()
        assert ad.click_or_heal(s, lambda: True, "x") is True and s.page.clicked is None, "fast path wins, no heal"
        assert ad.click_or_heal(s, lambda: False, "the generate button") is True, "miss -> heal"
        assert s.page.clicked == (321, 123), f"heal clicked vision coords: {s.page.clicked}"
        assert ad.click_or_heal(s, lambda: (_ for _ in ()).throw(RuntimeError()), "x") is True, "raise -> heal"
        globals()["find_control"] = lambda png, cands, what: None
        assert ad.click_or_heal(_Sess(), lambda: False, "absent") is False, "no control found -> False (caller walls)"
        print("OK: click_or_heal uses fast path, else self-heals via vision and clicks the coords")

        # live balance: the vision default passes a number through and fails open on anything else
        _orig_c = globals()["_credits_from_png"]
        try:
            globals()["_credits_from_png"] = lambda png: 42
            assert ad.read_credits(_Sess()) == 42, "observed balance passes through"
            globals()["_credits_from_png"] = lambda png: None
            assert ad.read_credits(_Sess()) is None, "unclear page -> None (keep the last known)"
            def _down(png):
                raise RuntimeError("gateway down")
            globals()["_credits_from_png"] = _down
            assert ad.read_credits(_Sess()) is None, "fails open, never raises into a job"
        finally:
            globals()["_credits_from_png"] = _orig_c
        print("OK: read_credits observes the page balance and fails open to None")
    finally:
        globals()["find_control"] = _orig


assert set(STATES)  # keep the import used; STATES is the poll vocabulary
