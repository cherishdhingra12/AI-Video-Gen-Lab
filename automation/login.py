"""
login.py — one-time manual login per site+account. Opens a real (headed) browser
on the site's home; you sign in by hand; cookies persist in profiles/<site>__<acct>/
so the runner is logged in forever after. Also seeds the credits row.

    python3 automation/login.py kling a1        # opens Kling, log in, press Enter
    python3 automation/login.py veo   personal   --credits 3

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("account")
    ap.add_argument("--credits", type=int, default=0,
                    help="seed today's free credit count for this account")
    args = ap.parse_args()

    home = importlib.import_module(f"sites.{args.site}").ADAPTER.home
    print(f"Opening {args.site} ({home}) for account '{args.account}'.")
    print("Log in fully in the window. When your dashboard/prompt box is visible,")
    print("just CLOSE the browser window — this saves your login and finishes.")
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
        st = State()
        st.set_credits(args.site, args.account, remaining=args.credits)
        st.close()
        print(f"  seeded {args.credits} credits for {args.site}/{args.account}")
    print("Done. Cookies saved — the runner will reuse this login.")


if __name__ == "__main__":
    main()
