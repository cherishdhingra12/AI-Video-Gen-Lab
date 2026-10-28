"""
login.py — one-time manual login per site+account. Opens a real (headed) browser
on the site's home; you sign in by hand; cookies persist in profiles/<site>__<acct>/
so the runner is logged in forever after. Also seeds the credits row.

    python3 automation/login.py kling a1        # opens Kling, log in, press Enter
    python3 automation/login.py veo   personal   --credits 3
    python3 automation/login.py googlevids a1 --check   # read the site's real balance, headless

Use different account labels (a1, a2, personal, work...) with different emails to
multiply the daily free ceiling. Re-run any time a session expires.
"""

import argparse
import importlib
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "sites"))

from browser import Session          # noqa: E402
from state import State              # noqa: E402


def check(site: str, account: str) -> int:
    """Read this site's REAL balance and record it, without opening a window.

    The scheduler only sees a site through its credits row, so a hand-entered number is the whole
    world: a stale `0` with no refill time is indistinguishable from an exhausted account and the
    site becomes invisible to `Brain.pick` forever. This is the way back — ask the site itself.

    Exit 0 when a balance was read and recorded, 3 when the page didn't say (nothing is written;
    an unclear read must never overwrite a good number — the same contract runner._observe_credits
    holds)."""
    adapter = importlib.import_module(f"sites.{site}").ADAPTER
    from runner import _refresh_cookies, read_balance
    if getattr(adapter, "needs_cookie_refresh", False):
        print(f"refreshing {site} cookies from Chrome first...")
        _refresh_cookies(site, account, getattr(adapter, "cookie_keywords", []))
    with Session(site, account, headless=True) as s:
        s.goto(adapter.home)
        n = read_balance(adapter, s)
    if n is None:
        print(f"{site}/{account}: the page did not state a balance — ledger left untouched.")
        st = State()
        print("  ledger says: " + st.credit_line(site, account))
        st.close()
        return 3
    st = State()
    st.observe_credits(site, account, n)
    print(f"{site}/{account}: {n} credits observed")
    print("  ledger now: " + st.credit_line(site, account))
    st.close()
    return 0


def seed(site: str, account: str, n: int, refill: str = None, st=None):
    """Record a balance a PERSON read off the site (or the site's own documented allowance).
    `refill` turns the site's stated period into its next reset time — without one a row that
    spends down to 0 leaves `resets_at=0`, and `available()` cannot tell that apart from a site
    that was never asked, so it drops the site for good."""
    resets = 0
    if refill:
        import learn
        resets = learn.refill_at(refill)
    own = st is None
    st = st or State()
    st.set_credits(site, account, remaining=n, resets_at=resets)
    if own:
        st.close()
    when = f", resets {time.strftime('%b %d', time.localtime(resets))}" if resets else ""
    print(f"seeded {n} credits for {site}/{account}{when}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("account")
    ap.add_argument("--credits", type=int, default=0,
                    help="seed today's free credit count for this account")
    ap.add_argument("--refill", choices=["daily", "monthly"], default=None,
                    help="with --credits: when this bucket comes back, so a spent-out row "
                         "knows its reset instead of sitting dead at resets_at=0")
    ap.add_argument("--check", action="store_true",
                    help="don't open a window: read the site's REAL balance and record it "
                         "(with --credits: record that number instead of reading)")
    args = ap.parse_args()

    if args.check:
        # --check --credits N = a person already knows the number (or it is the plan's own
        # allowance) and just wants it recorded. No window, no browser.
        if args.credits:
            seed(args.site, args.account, args.credits, args.refill)
            sys.exit(0)
        sys.exit(check(args.site, args.account))

    home = importlib.import_module(f"sites.{args.site}").ADAPTER.home
    target_url = home
    if args.site == "flow":
        target_url = "https://accounts.google.com/v3/signin/identifier?continue=https://flow.google.com/&flowName=GlifWebSignIn&flowEntry=ServiceLogin"

    print(f"Opening {args.site} ({target_url}) for account '{args.account}'.")
    print("Log in fully in the window. When your dashboard/prompt box is visible,")
    print("just CLOSE the browser window — this saves your login and finishes.")

    chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    profile_dir = os.path.abspath(os.path.join(_DIR, "..", "profiles", f"{args.site}__{args.account}"))
    os.makedirs(profile_dir, exist_ok=True)

    if os.path.exists(chrome_app):
        import subprocess
        print(f"Launching native Google Chrome (no automation flags) with profile: {profile_dir}")
        subprocess.run([chrome_app, f"--user-data-dir={profile_dir}",
                        "--no-first-run", "--no-default-browser-check", target_url])
        print("  window closed — saving session...")
    else:
        with Session(args.site, args.account, headless=False) as s:
            s.goto(home)
            # No stdin here (the harness's ! prefix has no interactive input), so we
            # wait for YOU to close the window instead of pressing Enter. 20-min safety cap.
            deadline = time.time() + 20 * 60
            while time.time() < deadline:
                if s.closed():
                    break
                time.sleep(1)
            print("  window closed — saving session...")
    if args.credits:
        seed(args.site, args.account, args.credits, args.refill)
    print("Done. Cookies saved — the runner will reuse this login.")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        # The credits ledger is what the scheduler sees, so a wrong write here is a site the
        # pipeline can never use — as happened: nine rows sat at a hand-entered 0 with no refill
        # time, and `available()` reads "0, no refill" as a PERMANENT dead end, so every one of
        # those working sites was invisible and a reel meant for Vids would have been shot on
        # vibes instead. What is checked here is that seeding says both halves of the fact.
        import tempfile
        from state import State as _State
        import learn as _learn
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        st = _State(db)
        seed("googlevids", "a1", 50, "monthly", st=st)
        r = st.db.execute("SELECT * FROM credits WHERE site='googlevids'").fetchone()
        assert r["remaining"] == 50, dict(r)
        assert r["resets_at"] > time.time(), \
            "a monthly seed must carry the month's reset — a 0 here is a site lost for good"
        st.set_credits("krea", "a1", 0, resets_at=0)         # the shape that killed the fleet
        sites = [r["site"] for r in st.available()]
        assert sites == ["googlevids"], \
            f"the seeded monthly site is schedulable, the 0/no-refill one is not: {sites}"
        st.spend_credit("googlevids", "a1", 50)
        assert not st.available(), \
            "spent to 0 with its refill still ahead is dry until then — unchanged"
        ago = st.credit_line("krea", "a1")
        assert "no refill time" in ago and "krea/a1" in ago, ago
        assert "no row" in st.credit_line("nope", "a1"), "an unseeded site must say so"
        st.close()

        # An unclear read is None, never a 0 — runner._observe_credits and login --check both
        # spend this one function, so the line is held once. A 0 written from a page that
        # simply did not say would delete the site (see above), which is the failure this whole
        # path exists to prevent.
        from runner import read_balance
        class _A:
            def __init__(self, fn): self.read_credits = fn
        assert read_balance(_A(lambda s: 7), None) == 7
        assert read_balance(_A(lambda s: None), None) is None, "no answer -> unknown, not zero"
        assert read_balance(_A(lambda s: True), None) is None, "True is not a balance"
        assert read_balance(_A(lambda s: -3), None) is None, "a negative read is not a balance"
        assert read_balance(_A(lambda s: (_ for _ in ()).throw(RuntimeError("gateway down"))), None) is None, \
            "a crashed read must not become a 0"
        assert read_balance(object(), None) is None, "an adapter that cannot read says nothing"
        print("OK: seeding records the balance AND its refill time; an unclear read stays None")
        sys.exit(0)
    main()
