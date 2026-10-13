"""
hailuo.py — Hailuo / MiniMax (hailuoai.video).

⚠ DISQUALIFIED 2026-09-22 under the daily-refresh-only rule. The free plan grants
"0 credits monthly" (subscribe page, verified) — the ~60 credits this account had
were a ONE-TIME signup grant, now spent. Clicking Create with 0 credits redirects
straight to /subscribe (that was the real cause of two 12-min "rendering" timeouts,
NOT a detection bug). Scheduler credits zeroed in state.db so the waterfall skips it.
Adapter kept (URL + /mine poll logic are correct) in case a paid/topped-up account
is ever used, but do NOT count Hailuo as a free daily bucket.

Second waterfall bucket. Selectors CONFIRMED live 2026-09-22.

Flow learned from the live UI (corrected 2026-09-22 after a timeout):
  - text-to-video entry is /create/text-to-video  (plain /create redirects to
    IMAGE-to-video, which needs an image — a prompt there generates nothing)
  - prompt box is a contenteditable DIV in the bottom "create bar"
  - settings pill "21:9 · 2K · 5s" opens aspect (has 9:16) + resolution + duration
  - the generate button reads "Create"
  - RESULTS DO NOT APPEAR ON THE CREATE PAGE. They land only under Assets = /mine.
    A finished tile shows a duration label + a real cdn .mp4 src; an in-progress
    one shows a "Creating" label and no real mp4 yet. So poll()/download() watch
    /mine, not the create page (whose only <video> is a static sample).
  - DOWNLOAD: open the newest /mine tile -> /my-work-detail/... -> the download
    icon (button.ant-dropdown-trigger, icon-only, bottom-right toolbar) fires it.
"""

import os
import time

from base import SiteAdapter, Wall

_POPUP_CLOSE = "[class*='close' i], .anticon-close, [aria-label*='close' i]"
_MINE = "https://hailuoai.video/mine"
_BUSY = ("creating", "generating", "processing", "queuing", "in queue", "waiting", "rendering")


class Hailuo(SiteAdapter):
    name = "hailuo"
    home = "https://hailuoai.video/create/text-to-video"   # NOT /create (=> image2video)
    credit_cost = 30                          # measure exactly on first live gen

    prompt_sel = "[contenteditable='true']"
    aspect = "9:16"
    resolution = "720P"

    def _dismiss(self, session):
        for x in session.page.query_selector_all(_POPUP_CLOSE):
            try:
                if x.is_visible():
                    x.click(timeout=800)
            except Exception:
                pass

    def _click_text(self, session, text, timeout=2500) -> bool:
        try:
            el = session.page.get_by_text(text, exact=True)
            if el.count():
                el.first.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    def _settings_pill(self, session):
        return session.page.evaluate_handle("""() => {
          const els=[...document.querySelectorAll('*')].filter(e=>e.childElementCount<=6);
          return els.find(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ');
            return /(21:9|16:9|9:16)/.test(t)&&/(2K|1080|720|768)/i.test(t)&&t.length<25;});}""").as_element()

    def _mine_mp4s(self, session) -> set:
        """The set of real finished-clip cdn .mp4 srcs currently on /mine."""
        return set(session.page.evaluate("""() => [...document.querySelectorAll('video')]
          .map(v => (v.currentSrc||v.src||''))
          .filter(s => /\\.mp4/i.test(s))""") or [])

    def configure(self, session):
        """9:16 + 720P via the settings pill; confirm 9:16 or raise (protect credits)."""
        pill = self._settings_pill(session)
        if pill:
            pill.click(timeout=3000)
            time.sleep(1.0)
            self._click_text(session, self.aspect)
            self._click_text(session, self.resolution)
            session.page.keyboard.press("Escape")
            time.sleep(0.8)
        pill2 = self._settings_pill(session)
        txt = ""
        try:
            txt = pill2.inner_text() if pill2 else ""
        except Exception:
            pass
        if self.aspect not in txt.replace(" ", ""):
            raise Wall("selector", f"could not confirm {self.aspect} (pill='{txt}')")

    def submit(self, session, prompt: str):
        # baseline: which finished clips already exist, so poll() can tell OURS apart
        session.goto(_MINE)
        time.sleep(3.0)
        self._base_mp4s = self._mine_mp4s(session)

        session.goto(self.home)
        time.sleep(2.0)
        self._dismiss(session)
        self._guard(session)
        self.configure(session)
        session.human_type(self.prompt_sel, prompt)
        # the generate button reads exactly "Create" (not "Recreate"/"Create Team")
        if not self._click_text(session, "Create"):
            session.human_click("button:has-text('Create')")
        time.sleep(2.0)

    def poll(self, session) -> str:
        """Watch /mine: done when a NEW finished .mp4 (not in baseline) shows up."""
        session.goto(_MINE)
        time.sleep(2.5)
        pg = session.page
        try:
            body = pg.inner_text("body")[:4000].lower()
            if any(k in body for k in ("insufficient", "not enough", "run out",
                                       "no credit", "quota")):
                raise Wall("credits", "insufficient credits")   # one-time signup grant, never refills
        except Wall:
            raise
        except Exception:
            body = ""
        new = self._mine_mp4s(session) - getattr(self, "_base_mp4s", set())
        if new:
            return "done"
        return "rendering"          # still building (tile shows a _BUSY label, no mp4 yet)

    def download(self, session, dest: str) -> str:
        """Open the newest /mine tile's detail view, click the download icon."""
        session.goto(_MINE)
        time.sleep(3.0)
        pg = session.page
        card = pg.query_selector("video")          # newest is first (top-left)
        if card:
            card.click(timeout=3000)
            time.sleep(2.5)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        # download icon = an icon-only button.ant-dropdown-trigger in the bottom toolbar
        for b in pg.query_selector_all("button.ant-dropdown-trigger, button"):
            try:
                bb = b.bounding_box()
                if not bb or bb["y"] < 780 or bb["x"] < 850:
                    continue
                if (b.inner_text() or "").strip():        # skip text buttons (Recreate)
                    continue
                with pg.expect_download(timeout=15000) as dl:
                    b.click(timeout=2000)
                dl.value.save_as(dest)
                return dest
            except Exception:
                continue
        raise Wall("selector", "could not trigger Hailuo download")


ADAPTER = Hailuo()
