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
from vision import read_state, STATES  # noqa: E402  (shared perception)


class Wall(Exception):
    """A blocker we should step back from. kind in captcha|rate_limit|login|block."""
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


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

    def _guard(self, session):
        """Vision check for a wall before we spend effort. Raise Wall if blocked."""
        st = read_state(session.screenshot())
        if st["state"] in ("captcha", "rate_limited", "login"):
            kind = {"rate_limited": "rate_limit"}.get(st["state"], st["state"])
            raise Wall(kind, st["reason"])


assert set(STATES)  # keep the import used; STATES is the poll vocabulary
