"""
import_login.py — reuse a login you ALREADY have in your real browser.

Google blocks "Sign in with Google" inside automated browsers, so instead of
logging in through Playwright we lift the site's session cookies from the real
Chrome/Safari where you're already logged in, and inject them into our persistent
profile. After this the headless runner is authenticated — no Chrome kept open, no
OAuth in automation. Also unlocks Google sites (Veo) by carrying google.com cookies.

    python3 automation/import_login.py kling a1 --browser chrome --credits 60
    python3 automation/import_login.py veo personal --browser safari

macOS will pop a Keychain prompt the first time (Chrome cookies are encrypted) —
click Allow. Read-only: it copies cookies, never changes your real browser.
"""

import argparse
import importlib
import os
import sys
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)
sys.path.insert(0, os.path.join(_DIR, "sites"))

import browser_cookie3 as bc          # noqa: E402
from browser import Session            # noqa: E402
from state import State                # noqa: E402
from vision import read_state          # noqa: E402


def _reg_domain(host: str) -> str:
    """app.klingai.com -> klingai.com (naive eTLD+1; fine for these sites)."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _backend(browser: str):
    return {"chrome": bc.chrome, "safari": bc.safari, "firefox": bc.firefox,
            "edge": bc.edge, "brave": bc.brave, "chromium": bc.chromium}[browser]


def _chrome_cookie_files() -> list:
    """Every Chrome profile's cookie DB (Default, Profile 1, ...). Your login may
    live in any of them, so we scan them all rather than only Default."""
    base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    files = []
    if os.path.isdir(base):
        for entry in sorted(os.listdir(base)):
            p = os.path.join(base, entry)
            if not os.path.isdir(p):
                continue
            for cand in ("Network/Cookies", "Cookies"):
                fp = os.path.join(p, cand)
                if os.path.exists(fp):
                    files.append((entry, fp))
                    break
    return files


def _match(jar, keywords) -> list:
    return [c for c in jar if any(k in (c.domain or "").lower() for k in keywords)]


def _gather_chrome(keywords: list, only: str = None):
    """Scan ALL Chrome profiles; return cookies from the profile with the most
    matches (mixing sessions across profiles would clash), plus a per-profile report.

    `only` pins one profile by name. That matters the moment a site has TWO accounts: account
    labels exist to multiply a free ceiling, and a second Google account lives in a second Chrome
    profile — but "most matches wins" hands back whichever profile happened to browse more, i.e.
    account 1's session, so `a2` would silently be `a1` and hit the same generation limit.
    """
    report, best_name, best = {}, None, []
    for name, fp in _chrome_cookie_files():
        if only and name != only:
            continue
        try:
            kept = _match(bc.chrome(cookie_file=fp, domain_name=""), keywords)
        except Exception as e:
            report[name] = f"err:{str(e)[:40]}"
            continue
        report[name] = len(kept)
        if len(kept) > len(best):
            best, best_name = kept, name
    return best, best_name, report


def _gather(browser: str, keywords: list):
    """Non-Chrome browsers: load all cookies, keep keyword-matching domains."""
    jar = _backend(browser)(domain_name="")
    return _match(jar, keywords)


def _to_pw(cookies) -> list:
    """browser_cookie3 Cookie -> Playwright add_cookies dict."""
    out = []
    for c in cookies:
        d = {"name": c.name, "value": c.value,
             "domain": c.domain, "path": c.path or "/",
             "secure": bool(getattr(c, "secure", False)),
             "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly", False))}
        if getattr(c, "expires", None):
            d["expires"] = int(c.expires)      # else session cookie
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("account")
    ap.add_argument("--browser", default="chrome",
                    choices=["chrome", "safari", "firefox", "edge", "brave", "chromium"])
    ap.add_argument("--credits", type=int, default=0)
    ap.add_argument("--keyword", action="append", default=[],
                    help="extra domain keyword(s) to match, e.g. --keyword kling")
    ap.add_argument("--profile", default=None,
                    help="pin which Chrome profile to import from (Default, 'Profile 1', ...) — "
                         "needed as soon as one site has two accounts, or account 1's session is "
                         "handed to account 2")
    args = ap.parse_args()

    adapter = importlib.import_module(f"sites.{args.site}").ADAPTER
    home = adapter.home
    host = home.split("//", 1)[-1].split("/", 1)[0]
    site_dom = _reg_domain(host)

    # match by keyword-in-domain so aliases (kling.ai vs klingai.com) all match.
    # keywords: the site name, the second-level of its domain, any --keyword given.
    keywords = {args.site.lower(), site_dom.split(".")[0].lower(), *[k.lower() for k in args.keyword]}
    if "google" in host:
        keywords.add("google")
    keywords = [k for k in keywords if k]

    got, prof = None, None
    try:
        if args.browser == "chrome":
            got, prof, report = _gather_chrome(keywords, only=args.profile)
            print(f"  chrome profiles scanned: {report}")
            if prof:
                print(f"  using profile '{prof}' ({len(got)} matching cookies)")
            if args.profile and not prof:
                sys.exit(f"no cookies for {keywords} in Chrome profile {args.profile!r}. "
                         f"Profiles scanned: {report or 'none'}. Sign that account in, or drop "
                         f"--profile to let it pick the fullest profile.")
        else:
            got = _gather(args.browser, keywords)
    except Exception as e:
        sys.exit(f"could not read {args.browser} cookies: {e} "
                 "(Safari needs Terminal to have Full Disk Access in System Settings)")
    pw_cookies = _to_pw(got)
    print(f"  {args.browser}: matched {len(pw_cookies)} cookies (keywords={keywords})")
    if not pw_cookies:
        sys.exit("not logged into this site in that browser. Easiest fix: open your "
                 "NORMAL Chrome and log into the site (Google sign-in works in a real "
                 "browser), then re-run this. Or add --keyword <part-of-the-domain>.")

    # inject into our persistent profile, then verify we land logged-in
    with Session(args.site, args.account, headless=True) as s:
        s._ctx.add_cookies(pw_cookies)
        if prof:
            # Remember WHICH Chrome profile this account's session came from. The runner re-imports
            # cookies before every run (Google rotates them fast), and without this note it would
            # re-scan every profile and hand back the fullest one — which, now that this site has
            # two accounts, is account 1's — silently signing a2 back out and into a1. A one-line
            # file next to the session it describes needs no registry to stay in sync.
            with open(os.path.join(s.profile, ".chrome-profile"), "w") as f:
                f.write(prof)
        s.goto(home)
        time.sleep(3)
        state = read_state(s.screenshot())["state"]
        shot = os.path.join(_DIR, f"import_{args.site}_{args.account}.png")
        with open(shot, "wb") as f:
            f.write(s.screenshot())
    print(f"  injected {len(pw_cookies)} cookies; landing page state={state}; shot={shot}")
    if state == "login":
        print("  ⚠ still shows login — the site may bind sessions to a device/UA. "
              "Tell me and we'll try --browser safari or a CDP attach.")

    if args.credits:
        st = State()
        st.set_credits(args.site, args.account, remaining=args.credits)
        st.close()
        print(f"  seeded {args.credits} credits for {args.site}/{args.account}")
    print("Done. If state was not 'login', the runner is now authenticated.")


if __name__ == "__main__":
    main()
