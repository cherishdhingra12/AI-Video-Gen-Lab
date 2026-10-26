"""
flow.py — Google Flow (flow.google.com), Google's creative studio on **Veo 3.1** (frontier,
the fleet's quality prize). DAILY refresh: ~50 Flow credits/day, don't roll over.

LOGIN: its own Google session. The shared google.com cookies expire between runs and the app
then redirects flow.google.com/ -> /about (signed out). Re-hydrate with:
    import_login.py flow a1 --browser chrome --keyword google
(pulls the active Chrome "Default" google cookies into profiles/flow__a1).

CONFIRMED live end-to-end 2026-09-23 (profile flow__a1, PRO account) — a real gen fired and a
720x1280 h264 8s MP4 was captured & ffprobe-verified:SUBMIT  home -> "New project" -> "Settings trigger" chip in the prompt bar (opens a mat-menu —
        was a fixed-coord panel; REBUILT for the new UI 2026-10-24) -> Video / 9:16 / 720p / 8s / x1
        (idempotent clicks; the menu also states the real credit cost, which is read here)
        -> Escape (close; else the overlay eats the click) -> type into
        div.ProseMirror[contenteditable] -> "Start generation".
        A 'what's new' change-log dialog occasionally opens over the project whose overlay
        swallows every click — dismissed in passing (see _dismiss_modal).
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
from vision import read_state


class Flow(SiteAdapter):
    name = "flow"
    home = "https://flow.google.com/"
    credit_cost = 12                 # the menu's own words: 'Generating will use 12 credits'
                                     # (Video · 720p · 8s · x1) — re-read live on every submit
    aspect = "9:16"
    needs_cookie_refresh = True      # Google session cookies rotate fast — re-import each run
    cookie_keywords = ["google"]

    _QUOTA = re.compile(r"(reached|out of|no more|ran out|not enough|insufficient).{0,40}"
                        r"(credit|generation|quota|limit)", re.I)
    _FAIL = re.compile(r"\b(failed|something went wrong|try again)\b", re.I)

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _clicked(ok: bool) -> str:
        return "1" if ok else "0"

    @staticmethod
    def _js_click(loc, timeout=4000) -> bool:
        """A click pointer overlays cannot block: real click first, then a DOM click().
        The change-log dialog's cdk-overlay once burned a 30s click timeout retrying
        against it (2026-10-24); el.click() goes straight through that glass."""
        try:
            loc.click(timeout=timeout)
            return True
        except Exception:
            pass
        try:
            loc.evaluate("el => el.click()")
            return True
        except Exception:
            return False

    @staticmethod
    def _click_role(session, name, timeout=8000):
        try:
            loc = session.page.get_by_role("button", name=re.compile(name, re.I)).first
            if loc.count():
                return Flow._js_click(loc, timeout)
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

    def _menu_click(self, session, pat: str) -> bool:
        """Click a text item inside the open settings overlay (cdk-overlay-container).
        Scoped so the trigger chip's own 'Video · 720p · 8s' text can never steal the click."""
        try:
            loc = session.page.locator(".cdk-overlay-container").get_by_text(
                re.compile(pat, re.I)).first
            if loc.count():
                return Flow._js_click(loc)
        except Exception:
            pass
        return False

    def _dismiss_modal(self, session):
        """A change-log dialog occasionally opens over the project and its cdk-overlay
        swallows EVERY point (2026-10-24: the prompt-box click burned its whole 30s timeout
        retrying against 'change-log-modal-actions' until the run errored). Dismiss it —
        its own action button, else Escape — before anything under it is touched."""
        pg = session.page
        try:
            dlg = pg.locator("mat-dialog-container")
            if not dlg.count():
                return
            try:
                pg.screenshot(path=f"/tmp/flow_changelog_{int(time.time())}.png")
            except Exception:                                 # evidence must never be the killer
                pass
            btns = dlg.first.get_by_role("button")
            if btns.count():
                btns.last.click(timeout=3000)
                time.sleep(1.2)
                if not pg.locator("mat-dialog-container").count():
                    return
            pg.keyboard.press("Escape"); time.sleep(1)
        except Exception:
            try:
                pg.keyboard.press("Escape"); time.sleep(1)
            except Exception:
                pass

    # ---- contract ----------------------------------------------------------
    def submit(self, session, prompt: str):
        step = print                                      # step trace: the site drifts, and a
        pg = session.page                                 # blind wall costs three retries of a branch
        session.goto(self.home)
        time.sleep(9)
        step(f"  [flow] landed at {pg.url}")
        # Signed out: flow.google.com serves the /about marketing shell ("Create with Google
        # Flow"). Google session cookies rotate fast, so a snapshot import only authenticates for a
        # short window — re-run import_login.py right before each run. NOTE: a missing "New project"
        # by itself is NOT treated as signed-out any more — the UI drifts, so that step self-heals
        # via vision below; only the /about shell or the marketing headline means truly logged out.
        if "/about" in pg.url or re.search(r"Create with Google Flow", self._body(session), re.I):
            raise Wall("login", "flow signed out; re-run: import_login.py flow a1 --browser chrome --keyword google")
        self._guard(session)
        if self._QUOTA.search(self._body(session)):
            raise Wall("credits", "Google Flow daily credits spent — refreshes next day",
                           refill="daily")

        if not self.click_or_heal(session, lambda: self._click_role(session, "New project"),
                                  "the 'New project' button or tile that starts a new video project"):
            raise Wall("selector", "Flow 'New project' not found — not even via vision self-heal")
        time.sleep(9)
        self._dismiss_modal(session)          # change-log dialog: seen once per session
        step("  [flow] new project open")

        # SETTINGS — the new UI (2026-10-24) hangs every generation option off the prompt-bar
        # chip 'Video · 720p · 8s / crop / x1' (still role 'Settings trigger'), which opens a
        # mat-menu holding Image|Video|Frames|Ingredients, 16:9|9:16, the model, 360p|720p,
        # 4s..10s and x1..x4 — plus the real credit line ('Generating will use 12 credits'),
        # read here so the ledger's affordability math uses the site's own number.
        if not self.click_or_heal(session, lambda: self._click_role(session, "Settings trigger"),
                                  "the generation-settings chip in the prompt bar"):
            raise Wall("selector", "Flow settings chip not found — not even via vision self-heal")
        time.sleep(2)
        m = re.search(r"(\d+)\s*credits", self._body(session))
        if m and int(m.group(1)) > 0:
            self.credit_cost = int(m.group(1))            # observed like every other number
        got = [(pat, self._menu_click(session, pat))
               for pat in (r"\bVideo\b", r"\b9:16\b", r"\b720p\b", r"\b8s\b", r"\bx1\b")]
        for pat, ok in got:
            if not ok:
                # a selection can close the menu — reopen the chip and try once more
                self._click_role(session, "Settings trigger"); time.sleep(1.5)
                self._menu_click(session, pat)
            time.sleep(0.6)
        step("  [flow] settings: " + " ".join(
            f"{pat}={self._clicked(ok)}" for pat, ok in got))
        pg.keyboard.press("Escape"); time.sleep(1.2)  # close menu or the overlay eats the click
        self._dismiss_modal(session)          # it can also appear right before typing

        box = pg.locator("div.ProseMirror[contenteditable=true]").last
        if box.count():
            if not Flow._js_click(box):
                box.click()
        elif not self.heal_click(session, "the main prompt text box where the video description is typed"):
            raise Wall("selector", "Flow prompt box (ProseMirror) not found — not even via vision self-heal")
        time.sleep(0.5)
        pg.keyboard.type(prompt, delay=14); time.sleep(1)
        step(f"  [flow] prompt typed ({len(prompt)} chars); typed-visible={re.search(r'.{18}', self._body(session)) is not None}")

        try:
            pg.screenshot(path=f"/tmp/flow_prestart_{int(time.time())}.png")  # evidence at the last place a wall can live
        except Exception:
            pass
        if not self.click_or_heal(session, lambda: self._click_role(session, "Start generation"),
                                  "the 'Start generation' / generate / create button that submits the prompt"):
            title = pg.get_by_role("button", name=re.compile("Start generation", re.I)).count()
            pg.screenshot(path=f"/tmp/flow_miss_{int(time.time())}.png")
            step(f"  [flow] Start generation NOT FOUND (role count={title})")
            raise Wall("selector", "Flow 'Start generation' not found — not even via vision self-heal")
        step("  [flow] generation submitted")
        time.sleep(4)
        if self._QUOTA.search(self._body(session)):
            raise Wall("credits", "Google Flow daily credits spent — refreshes next day",
                           refill="daily")

    def poll(self, session) -> str:
        pg = session.page
        body = self._body(session)
        if self._QUOTA.search(body):
            return "rate_limited"
        if self._FAIL.search(body):
            v = read_state(session.screenshot()).get("state")   # vision double-check before giving up
            return v if v in ("rendering", "done", "rate_limited") else "error"
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
