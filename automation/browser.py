"""
browser.py — the hands and eyes of the engine.

A persistent Chromium context per account: cookies/localStorage live on disk under
profiles/<account>/, so a one-time manual login sticks forever across restarts.
Everything a site adapter needs to LOOK human is here, so the adapters stay tiny.

Human-like on purpose (bans come from looking robotic):
  - real cursor paths (Bezier-ish steps) before a click, not teleport-and-click
  - typing with per-key jitter, not instant value-set
  - randomized think-time pauses
  - the standard automation tells (navigator.webdriver etc.) patched off

    with Session("kling", "a1", headless=False) as s:
        s.goto("https://app.klingai.com")
        s.human_type("textarea", "a lantern drifting through fog")
        s.human_click("button:has-text('Generate')")
        png = s.screenshot()                 # bytes -> vision.py
        s.wait_download("button.download", "/out/clip.mp4")
"""

import os
import random
import time

from playwright.sync_api import sync_playwright

_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES = os.path.join(os.path.dirname(_DIR), "profiles")

# a real, current UA so we don't ship Playwright's HeadlessChrome default
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# strip the obvious "I am automated" signals before any page script runs
_STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
window.chrome={runtime:{}};
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
"""


def _pause(a=0.4, b=1.3):
    time.sleep(random.uniform(a, b))


class Session:
    def __init__(self, site: str, account: str, headless: bool = False,
                 profile_root: str = PROFILES):
        self.site, self.account = site, account
        self.profile = os.path.join(profile_root, f"{site}__{account}")
        os.makedirs(self.profile, exist_ok=True)
        self.headless = headless
        self._pw = self._ctx = self.page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        # persistent context = the profile dir holds cookies => login survives.
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile, headless=self.headless, user_agent=UA,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        self._ctx.add_init_script(_STEALTH)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self._ctx.close()          # persists cookies to the profile dir
        except Exception:
            pass                        # window may already be closed by the user
        finally:
            self._pw.stop()

    def closed(self) -> bool:
        """True once the user has closed the browser window (no pages left)."""
        try:
            return len(self._ctx.pages) == 0
        except Exception:
            return True

    # ---- navigation ----
    def goto(self, url: str, wait="domcontentloaded"):
        self.page.goto(url, wait_until=wait, timeout=60000)
        _pause(1.0, 2.5)                       # let the SPA settle, look like reading

    # ---- human-like input ----
    def _move_to(self, sel: str):
        el = self.page.wait_for_selector(sel, timeout=30000, state="visible")
        box = el.bounding_box()
        if box:
            # a few intermediate points so the cursor arcs in, not teleports
            tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            steps = random.randint(6, 14)
            self.page.mouse.move(tx, ty, steps=steps)
            _pause(0.15, 0.5)
        return el

    def _click_el(self, el):
        """Click, tolerating SPA overlays. The human mouse-arc already happened in
        _move_to; here we try a trusted click but DON'T hang 30s if a transparent
        sibling panel intercepts the hit (common on these video SPAs) — fall back to
        a JS dispatch, which lands on the element itself. Anti-ban motion is kept;
        only the final delivery changes when the page is fighting us."""
        try:
            el.click(timeout=4000)
            return
        except Exception:
            pass
        try:
            el.evaluate("e => { e.scrollIntoView({block:'center'}); e.click(); }")
        except Exception:
            el.click(force=True, timeout=4000)      # last resort

    def human_click(self, sel: str):
        el = self._move_to(sel)
        _pause(0.1, 0.4)
        self._click_el(el)
        _pause()

    def human_type(self, sel: str, text: str):
        el = self._move_to(sel)
        self._click_el(el)                          # focus (overlay-tolerant)
        _pause(0.1, 0.3)
        try:
            el.focus()                              # ensure caret even if JS-clicked
        except Exception:
            pass
        for ch in text:
            self.page.keyboard.type(ch)
            time.sleep(random.uniform(0.02, 0.12))   # per-key jitter
        _pause()

    # ---- perception ----
    def screenshot(self, full: bool = False) -> bytes:
        return self.page.screenshot(full_page=full)

    def has(self, sel: str, timeout: float = 2000) -> bool:
        try:
            self.page.wait_for_selector(sel, timeout=timeout, state="visible")
            return True
        except Exception:
            return False

    def text(self) -> str:
        try:
            return self.page.inner_text("body")[:8000]
        except Exception:
            return ""

    # ---- download ----
    def wait_download(self, trigger_sel: str, dest: str, timeout: float = 600000) -> str:
        """Click the trigger and save whatever download it starts to `dest`."""
        with self.page.expect_download(timeout=timeout) as dl:
            self._move_to(trigger_sel).click()
        d = dl.value
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        d.save_as(dest)
        return dest


if __name__ == "__main__":
    # self-check (headless, no network): stealth patch applied + human_type really
    # lands text in a field. Proves the perception/input plumbing without a login.
    with Session("selftest", "a0", headless=True) as s:
        s.page.set_content(
            "<textarea id='t'></textarea><button id='b'>go</button>"
            "<div id='out' style='display:none'>ok</div>"
            "<script>b.onclick=()=>out.style.display='block'</script>")
        assert s.page.evaluate("navigator.webdriver") in (None, False), "webdriver not hidden"
        s.human_type("#t", "hello world")
        assert s.page.input_value("#t") == "hello world", "human_type did not land text"
        assert not s.has("#out", timeout=300), "out should be hidden pre-click"
        s.human_click("#b")
        assert s.has("#out", timeout=1000), "human_click did not fire handler"
        shot = s.screenshot()
        assert shot[:8] == b"\x89PNG\r\n\x1a\n", "screenshot not a PNG"
    print("OK: stealth hides webdriver, human type+click work, screenshot is PNG")
