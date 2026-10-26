"""
pool.py — the Google account pool: three accounts, one cycle, an honest credit count.

The owner's setup (2026-10-22): three Google accounts on Vids AND Flow. When one is exhausted
the load moves to the next, in a CYCLE, and the machine heals itself. And — the part that
matters most — the credits left in each account must be tracked ACCURATELY, because Google Vids
shows no balance anywhere (confirmed live 2026-10-22: the home page, /settings, /account and
/usage all render the same body with no counter; labs.google/fx/tools/veo is deprecated).

So the count is DERIVED, not read:

  * `pool`   — how many generations the bucket holds (~50/month for Vids). Learned from the
               first refusal: an account that has spent `used` and is then refused had exactly
               `used` to spend.
  * `used`   — generations made since the bucket came back. Every render decrements it.
  * `remaining` — pool - used, once the pool size is known.

Until a refusal has happened the count is a hand-entered estimate and says so.

The cycle needs no separate cursor: `used_at` says who ran last, so the account that has been
idle longest goes next (state.rotation). An account that is dry, cooling or signed out is
skipped, and the next one takes the job.

    python3 automation/pool.py --list                 # what the ledger believes, per account
    python3 automation/pool.py --setup                # register the three accounts on both sites
    python3 automation/pool.py --sync                 # read the site, update what is true
    python3 automation/pool.py --pin a2 "Profile 3"   # pin an account to a Chrome profile

The pin matters: import_login.py picks the Chrome profile with the MOST cookies, which is
account 1's — so account 2 would be handed account 1's session and would spend account 1's
generation limit while the ledger counted it against account 2. Every account therefore needs
its own Chrome profile, and the pin is what says which.
"""

import argparse
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "sites"))   # the adapters import `base` by name

from state import State        # noqa: E402

# The three Google accounts, in the order they should be used. The label is the account's name
# in the ledger; the email is the address the session must actually hold (checked live against
# the site's own account avatar before a credit is spent).
GOOGLE_ACCOUNTS = [
    ("a1", "cherishdhingra12@gmail.com"),
    ("a2", "cherishdhingra1234@gmail.com"),
    ("a3", "cherishdhingra1@gmail.com"),
]

# The two Google sites that share these sessions.
GOOGLE_SITES = ["googlevids", "flow"]

# The bucket size each site's account is given, when the site does not show one.
# Google Vids: ~50 AI-clip generations a month (documented, and measured — the account hit the
# limit on 2026-10-16 after ~50 generations). Google Flow: ~50 credits a day, and one Veo
# 720p/8s generation costs ~20 of them.
POOL_SIZE = {"googlevids": 50, "flow": 50}


def setup(st: State = None, sites=None) -> None:
    """Register every Google account on every Google site: the email it must hold, and the
    bucket it draws from. Does NOT create a session — a person still has to sign each account
    in (or the Chrome cookies have to be imported); this only tells the ledger what to expect.

    An account that is NEW, or whose refill time has already passed, starts with the WHOLE
    bucket: a row that sits at 0 with a passed refill date is read by `available()` as "back",
    and it must then be able to actually spend — otherwise the pool says the account returned
    and the scheduler finds it empty."""
    own = st is None
    st = st or State()
    now = time.time()
    for site in (sites or GOOGLE_SITES):
        for label, email in GOOGLE_ACCOUNTS:
            size = POOL_SIZE.get(site, 0)
            st.set_pool(site, label, size, email=email)
            row = st.db.execute("SELECT remaining, pool, resets_at, source FROM credits "
                                "WHERE site=? AND account=?", (site, label)).fetchone()
            fresh = row["remaining"] == 0 and (not row["resets_at"] or row["resets_at"] <= now)
            if fresh and size:
                st.set_credits(site, label, size, source="manual")
            print(f"  {site}/{label} <- {email}  (pool {size})")
    if own:
        st.close()


def listing(st: State = None) -> None:
    """What the ledger believes about every account — the honest version, with the size of the
    pool next to the number left in it and where the number came from."""
    own = st is None
    st = st or State()
    for site in GOOGLE_SITES:
        print(f"\n{site}")
        for label, email in GOOGLE_ACCOUNTS:
            row = st.db.execute("SELECT * FROM credits WHERE site=? AND account=?",
                                (site, label)).fetchone()
            if not row:
                print(f"  {label}: not registered — run: pool.py --setup")
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["resets_at"])) \
                if row["resets_at"] else "no refill time"
            left = f"{row['remaining']} of {row['pool']}" if row["pool"] else \
                   f"{row['remaining']} (pool size unknown)"
            print(f"  {label}: {left} left | used {row['used']} | resets {when} | "
                  f"{row['source']} | {email}")
            pin = _pin(site, label)
            if pin:
                print(f"       Chrome profile: {pin}")
    if own:
        st.close()


def _pin(site: str, account: str) -> str:
    root = os.path.dirname(_DIR)
    try:
        with open(os.path.join(root, "profiles", f"{site}__{account}", ".chrome-profile")) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_pin(site: str, account: str, profile: str) -> None:
    """Pin this account to one Chrome profile, so the cookie import takes THAT profile's
    session and no other. Without it the import takes the fullest profile — account 1's — and
    account 2 silently becomes account 1."""
    root = os.path.dirname(_DIR)
    d = os.path.join(root, "profiles", f"{site}__{account}")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".chrome-profile"), "w") as f:
        f.write(profile)
    print(f"  {site}/{account} pinned to Chrome profile '{profile}'")


def sync(st: State = None) -> None:
    """Ask each site which account its session holds, and record the answer. This is what
    turns a signed-out session into a fact in the ledger instead of a mystery — and the
    identity a session holds is also how the pool's per-account counts stay truthful."""
    own = st is None
    st = st or State()
    import importlib
    from browser import Session
    for site in GOOGLE_SITES:
        adapter = importlib.import_module(f"sites.{site}").ADAPTER
        for label, email in GOOGLE_ACCOUNTS:
            try:
                with Session(site, label, headless=True) as s:
                    s.goto(adapter.home)
                    got = adapter.read_account(s)
            except Exception as e:                                   # noqa: BLE001
                print(f"  {site}/{label}: could not read the session ({str(e)[:70]})")
                continue
            if not got:
                print(f"  {site}/{label}: the page does not say which account it is")
                continue
            ok = st.account_matches(site, label, got)
            print(f"  {site}/{label}: signed in as {got}"
                  f"{'' if ok else '  <-- NOT the account this label belongs to'}")
    if own:
        st.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="what the ledger believes, per account")
    ap.add_argument("--setup", action="store_true", help="register the three accounts on both sites")
    ap.add_argument("--sync", action="store_true", help="ask each site which account it holds")
    ap.add_argument("--pin", nargs=2, metavar=("ACCOUNT", "PROFILE"),
                    help="pin an account to a Chrome profile (e.g. a3 'Profile 5')")
    args = ap.parse_args()
    if args.pin:
        account, profile = args.pin
        for site in GOOGLE_SITES:
            set_pin(site, account, profile)
        return
    if args.setup:
        print("registering the Google pool:")
        setup()
        return
    if args.sync:
        print("asking each site which account it is signed in as:")
        sync()
        return
    listing()


if __name__ == "__main__":
    main()
