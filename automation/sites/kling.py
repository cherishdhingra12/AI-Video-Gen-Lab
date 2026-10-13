"""
kling.py — Kling AI adapter (kling.ai). Generous daily free credits, frontier-class.

SELECTORS ARE BEST-EFFORT and are the ONE thing to confirm on the first live,
logged-in run — Kling's a React SPA and class names change. When they drift, this
is the only file to fix (that's the whole point of the adapter split). The runner's
vision fallback (poll -> read_state) keeps working even while a selector is stale,
so a wrong selector degrades to "slower", not "broken".

Confirm on first run:
  - prompt_sel     the text-to-video prompt box
  - generate_sel   the Generate/Create button
  - download_sel   the finished-clip download control
Update, re-run the loop, done.
"""

import os
import time

from base import SiteAdapter, Wall

# promo/onboarding modals Kling throws up; dismiss best-effort before interacting
_POPUPS = ["Got It", "Got it", "Accept", "I Agree", "Confirm"]


class Kling(SiteAdapter):
    name = "kling"
    home = "https://app.klingai.com/global/text-to-video/new"
    credit_cost = 10                        # 66 free credits/day observed; ~10/gen

    # CONFIRMED against the live logged-in UI (2026-09-21):
    #  - prompt box is a TipTap contenteditable div, NOT a textarea
    #  - Generate is the only button whose text is exactly "Generate"
    prompt_sel = ".tiptap.ProseMirror"
    generate_sel = "button:has-text('Generate')"
    download_sel = ("a[download], button:has-text('Download'), "
                    "[class*='download' i]")     # confirm on first finished render

    # ECONOMY settings (the big cost lever). VIDEO 3.0 + 1080p + Native Audio cost
    # ~60 credits/clip; VIDEO 1.6 @ 720p has no native audio and is far cheaper, so
    # the free tier stretches to many clips instead of ~1/day. 9:16 = vertical reels.
    target_model = "VIDEO 1.6"
    aspect = "9:16"
    resolution = "720p"

    def _top_video_src(self, session):
        try:
            v = session.page.query_selector(".creation-stream-item video, video")
            return v.get_attribute("src") if v else None
        except Exception:
            return None

    def _click_text(self, session, text, timeout=2500) -> bool:
        try:
            el = session.page.get_by_text(text, exact=True)
            if el.count():
                el.first.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    def _open_model(self, session):
        h = session.page.evaluate_handle("""() => {
          const sp=[...document.querySelectorAll('span.name-new')].find(x=>/VIDEO/i.test(x.innerText));
          let n=sp; for(let i=0;i<6&&n;i++){ if(n.className&&/select|model|dropdown|trigger|header|card/i.test(n.className)) return n; n=n.parentElement;}
          return sp?sp.parentElement:null; }""")
        el = h.as_element()
        if el:
            el.click(timeout=3000)

    def _pick_model(self, session, name) -> bool:
        for li in session.page.query_selector_all("li.el-select-dropdown__item.option"):
            try:
                if name.lower() in (li.inner_text() or "").lower():
                    li.click(timeout=2500)
                    return True
            except Exception:
                continue
        return False

    def _pill(self, session):
        # the resolution/aspect pill, NOT the row that also holds Generate: match
        # res+aspect, exclude 'generate', and take the most specific (fewest kids).
        return session.page.evaluate_handle("""() => {
          const ok=[...document.querySelectorAll('*')].filter(e=>{
            const t=(e.innerText||'').replace(/\\s+/g,' ');
            return /(1080p|720p)/.test(t)&&/(16:9|9:16|1:1)/.test(t)
                   &&!/generate/i.test(t)&&t.length<30;});
          ok.sort((a,b)=>a.querySelectorAll('*').length-b.querySelectorAll('*').length);
          return ok[0]||null; }""").as_element()

    def _pill_text(self, session) -> str:
        el = self._pill(session)
        try:
            return (el.inner_text() or "").replace("\n", " ") if el else ""
        except Exception:
            return ""

    def configure(self, session):
        """Set economy model + 9:16 BEFORE generating, and CONFIRM it took. If we
        can't confirm cheap+vertical settings we raise (never burn ~60 credits on a
        misconfigured expensive clip)."""
        self._open_model(session)
        time.sleep(1.0)
        if not self._pick_model(session, self.target_model):
            self._click_text(session, "More")       # older models are behind "More"
            time.sleep(0.6)
            self._pick_model(session, self.target_model)
        time.sleep(1.2)

        pill = self._pill(session)
        if pill:
            pill.click(timeout=3000)
            time.sleep(1.0)
            self._click_text(session, self.aspect)
            self._click_text(session, self.resolution)
            session.page.keyboard.press("Escape")
            time.sleep(0.8)

        txt = self._pill_text(session)
        if self.aspect not in txt:
            raise Wall("selector", f"could not confirm {self.aspect}/economy (pill='{txt}')")

    def submit(self, session, prompt: str):
        session.goto(self.home)
        for t in _POPUPS:                        # clear onboarding modal if shown
            try:
                el = session.page.get_by_text(t, exact=True)
                if el.count():
                    el.first.click(timeout=1500)
                    break
            except Exception:
                pass
        self._guard(session)
        self.configure(session)                  # economy + 9:16, confirmed or raises
        # remember the newest existing clip so poll() can tell OURS apart from it
        self._base_src = self._top_video_src(session)
        session.human_type(self.prompt_sel, prompt)
        session.human_click(self.generate_sel)

    def poll(self, session) -> str:
        """Done when a NEW clip appears at the top (server-side render finishes).
        Cheap DOM check; vision only to catch a wall we'd otherwise miss."""
        pg = session.page
        # low-credit / quota toast?
        try:
            body = pg.inner_text("body")[:4000].lower()
            if any(k in body for k in ("insufficient", "not enough credit", "quota",
                                       "run out of", "credits are used up")):
                raise Wall("credits", "insufficient credits toast", refill="daily")
        except Wall:
            raise
        except Exception:
            pass
        top = self._top_video_src(session)
        if top and top != getattr(self, "_base_src", None):
            return "done"
        # occasional vision look for captcha/login the DOM check can't see
        if read_state_gate():
            st = self._guard_state(session)
            if st in ("captcha", "rate_limited", "login"):
                raise Wall({"rate_limited": "rate_limit"}.get(st, st), "seen while polling")
        return "rendering"

    def _guard_state(self, session):
        from base import read_state
        return read_state(session.screenshot())["state"]

    def download(self, session, dest: str) -> str:
        """Kling's download is a hover dropdown -> 'MP4'. Open it on the newest
        result and save the file. (Confirmed working live 2026-09-21.)"""
        pg = session.page
        for tr in pg.query_selector_all(".el-dropdown"):
            try:
                tr.hover(timeout=1500)
            except Exception:
                continue
            time.sleep(0.7)
            menu = pg.query_selector(".download-dropdown")
            if menu and menu.is_visible():
                opt = (pg.query_selector(".download-dropdown li:has-text('MP4')")
                       or pg.query_selector(".download-option-item:has-text('MP4')"))
                if not opt:
                    continue
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with pg.expect_download(timeout=300000) as dl:
                    opt.click()
                dl.value.save_as(dest)
                return dest
        raise Wall("selector", "could not open Kling download dropdown")


# light throttle so poll() only spends a vision call every ~5th check
_gate = {"n": 0}
def read_state_gate() -> bool:
    _gate["n"] += 1
    return _gate["n"] % 5 == 0


ADAPTER = Kling()
