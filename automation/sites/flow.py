"""
flow.py — Google Flow (flow.google.com), Google's creative studio on **Veo 3.1** (frontier,
the fleet's quality prize). DAILY refresh: ~50 Flow credits/day, don't roll over.

LOGIN: its own Google session. The shared google.com cookies expire between runs and the app
then redirects flow.google.com/ -> /about (signed out). Re-hydrate with:
    import_login.py flow a1 --browser chrome --keyword google
(pulls the active Chrome "Default" google cookies into profiles/flow__a1).

CONFIRMED live end-to-end 2026-09-23 (profile flow__a1, PRO account) — a real gen fired and a
720x1280 h264 8s MP4 was captured & ffprobe-verified:

SUBMIT  home -> "New project" -> "Settings trigger" (config panel) -> "Video" -> "9:16" -> "x1"
        (one output, conserves credits) -> Escape (close panel; else cdk-overlay eats the click)
        -> type into div.ProseMirror[contenteditable] -> "Start generation".
POLL    done = a PORTRAIT poster <img> (h>180, h>w, w>90) in the content area AND no "%" left in
        body (progress gone). Flow renders the result as an <img> poster, NOT a <video>. ~35-40s
        for 720p/8s. Failure text (failed|error|try again) -> error.
DOWNLOAD click the poster center -> clip detail view -> "Download media" button -> format menu.
        Pick "720p Original size" (native, IMMEDIATE mp4). "*Upscaled" items kick off an ASYNC
        render and deliver no file. Click the menu ITEM CENTER by live bbox (a text-node click
        misses) inside expect_download.
"""

import os
import re
import time

from base import SiteAdapter, Wall


class Flow(SiteAdapter):
    name = "flow"
    home = "https://flow.google.com/"
    credit_cost = 20                 # a Veo 720p/8s gen ~ 10-20 of the ~50 daily credits
    aspect = "9:16"
    needs_cookie_refresh = True      # Google session cookies rotate fast — re-import each run
    cookie_keywords = ["google"]

    _QUOTA = re.compile(r"(reached|out of|no more|ran out|not enough|insufficient).{0,40}"
                        r"(credit|generation|quota|limit)", re.I)
    _FAIL = re.compile(r"\b(failed|something went wrong|try again)\b", re.I)

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _click_role(session, name, timeout=8000):
        try:
            loc = session.page.get_by_role("button", name=re.compile(name, re.I)).first
            if loc.count():
                loc.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _role_present(session, name):
        try:
            return session.page.get_by_role("button", name=re.compile(name, re.I)).first.count() > 0
        except Exception:
            return False

    @staticmethod
    def _click_text(session, pat, timeout=5000, fallback=None):
        """Click first visible element matching `pat`; fall back to a fixed coord if given."""
        try:
            loc = session.page.get_by_text(re.compile(pat, re.I)).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=timeout)
                return True
        except Exception:
            pass
        if fallback:
            try:
                session.page.mouse.click(*fallback)
                return True
            except Exception:
                pass
        return False

    @staticmethod
    def _poster_box(pg):
        """bbox of a portrait result poster (Flow renders results as <img>, ~140px wide)."""
        im = pg.locator("img")
        for i in range(im.count()):
            b = im.nth(i).bounding_box()
            if b and b["height"] > 180 and b["height"] > b["width"] and b["width"] > 90:
                return b
        return None

    @staticmethod
    def _menu_item(pg, pat):
        """Center xy of the download-menu item whose text matches `pat` (compiled).
        Located by live bbox — clicking the text node itself does not register."""
        for o in pg.evaluate(
            """() => [...document.querySelectorAll('*')].map(e=>{const r=e.getBoundingClientRect();
                return {t:(e.textContent||'').replace(/\\s+/g,' ').trim(),
                        x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                        w:Math.round(r.width), h:Math.round(r.height)};})
              .filter(o=>o.w>40&&o.w<320&&o.h>18&&o.h<60)"""):
            if pat.search(o["t"]) and len(o["t"]) < 26:
                return o["x"], o["y"]
        return None

    def _body(self, session):
        try:
            return session.page.inner_text("body")
        except Exception:
            return ""

    # ---- contract ----------------------------------------------------------
    def submit(self, session, prompt: str):
        pg = session.page
        session.goto(self.home)
        time.sleep(9)
        # Signed out: flow.google.com serves the /about marketing shell ("Create with Google
        # Flow", no "New project"). Google session cookies rotate fast, so a snapshot import
        # only authenticates for a short window — re-run import_login.py right before each run.
        if ("/about" in pg.url
                or re.search(r"Create with Google Flow", self._body(session), re.I)
                or not self._role_present(session, "New project")):
            raise Wall("login", "flow signed out; re-run: import_login.py flow a1 --browser chrome --keyword google")
        self._guard(session)
        if self._QUOTA.search(self._body(session)):
            raise Wall("rate_limit", "Google Flow daily credits spent — refreshes next day")

        if not self._click_role(session, "New project"):
            raise Wall("selector", "Flow 'New project' not found — confirm live UI")
        time.sleep(9)

        # config panel: aspect 9:16 (Flow defaults 16:9 — REQUIRED) + single output.
        if not self._click_role(session, "Settings trigger"):
            raise Wall("selector", "Flow 'Settings trigger' (config panel) not found")
        time.sleep(1.5)
        self._click_text(session, r"videocam Video|\bVideo\b", fallback=(817, 627)); time.sleep(0.8)
        if not self._click_text(session, r"9:16", fallback=(859, 669)):
            raise Wall("selector", "Flow 9:16 aspect not settable — refusing (output must be vertical)")
        time.sleep(0.8)
        try:                                          # x1 = one output, conserves credits
            pg.get_by_text("x1", exact=True).first.click(timeout=3000)
        except Exception:
            pass
        pg.keyboard.press("Escape"); time.sleep(1)    # close panel or cdk-overlay eats the click

        box = pg.locator("div.ProseMirror[contenteditable=true]").last
        if not box.count():
            raise Wall("selector", "Flow prompt box (ProseMirror) not found")
        box.click(); time.sleep(0.5)
        pg.keyboard.type(prompt, delay=14); time.sleep(1)

        if not self._click_role(session, "Start generation"):
            raise Wall("selector", "Flow 'Start generation' not found / disabled")
        time.sleep(4)
        if self._QUOTA.search(self._body(session)):
            raise Wall("rate_limit", "Google Flow daily credits spent — refreshes next day")

    def poll(self, session) -> str:
        pg = session.page
        body = self._body(session)
        if self._QUOTA.search(body):
            return "rate_limited"
        if self._FAIL.search(body):
            return "error"
        if "%" in body:                               # progress indicator still up
            return "rendering"
        return "done" if self._poster_box(pg) else "rendering"

    def download(self, session, dest: str, timeout: float = 90.0) -> str:
        pg = session.page
        box = self._poster_box(pg)
        if not box:
            raise Wall("selector", "Flow result poster not found for download")
        pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)  # -> detail view
        time.sleep(3)
        if not self._click_role(session, "Download media"):
            raise Wall("selector", "Flow 'Download media' button not found in detail view")
        time.sleep(2.5)
        item = self._menu_item(pg, re.compile(r"Original size", re.I))   # native immediate mp4
        if not item:
            raise Wall("selector", "Flow '720p Original size' menu item not found")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with pg.expect_download(timeout=int(timeout * 1000)) as di:
                pg.mouse.click(*item)
            di.value.save_as(dest)
        except Exception as e:
            raise Wall("block", f"Flow MP4 download did not deliver: {str(e)[:100]}")
        return dest


ADAPTER = Flow()
