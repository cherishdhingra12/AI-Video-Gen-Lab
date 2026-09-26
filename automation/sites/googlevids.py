"""
googlevids.py — Google Vids (vids.google.com), Veo 3.1 clips. MONTHLY-refresh, now
in-scope (policy 2026-09-23: monthly accepted). The QUALITY pick of the fleet — Veo 3.1,
~50 AI-clip gens/month, resets 12 AM PT on the 1st. 9:16 vertical supported since Oct 2025.

FLOW (CONFIRMED live 2026-09-23 on profile googlevids__a1 — cookies imported via
import_login.py; selectors exercised headless up to the enabled Generate button, NO gen
fired during recon):
  1. Home '+' ("Start a new video") -> "Let's start creating" dialog.
  2. Dialog aspect toggle -> "Portrait" (sets canvas + clip to 9:16 vertical).
  3. "Create AI videos" tile (Omni/Veo) -> editor opens with the AI Video right-rail panel
     in Create mode, aspect already Portrait.
  4. Type prompt in the panel prompt box (textarea/contenteditable). The send arrow
     (aria-label='Generate') is disabled until the prompt is non-empty; click it to fire
     (spends 1 of the ~50 monthly clip gens; Veo render).
  5. Poll until the clip renders (vision), then export/download the vid as mp4.

Quota gate: when the monthly pool is spent, Vids shows a limit message → Wall("rate_limit")
so the runner cools it (ideally until the 1st; see learn.py note in memory). EXPORT/DOWNLOAD
path (File -> Download) is NOT yet mapped against a finished render — same posture as
TensorArt: submit proven, download pending the first finished gen.
"""

import os
import re
import time

from base import SiteAdapter, Wall


class GoogleVids(SiteAdapter):
    name = "googlevids"
    home = "https://vids.google.com/"
    credit_cost = 1                          # 1 AI-clip gen; ~50/month
    aspect = "9:16"
    needs_cookie_refresh = True              # Google session cookies rotate fast — re-import
    cookie_keywords = ["google"]             # each run (same class as Flow; expired -> account chooser)

    # text-based selectors so they survive class-name churn; CONFIRMED live 2026-09-23.
    prompt_sel = "textarea, [contenteditable='true']"     # AI-clip panel prompt box (1 match)
    generate_sel = "[aria-label='Generate' i]"            # send arrow; disabled until prompt non-empty
    download_sel = ("[aria-label*='download' i], a[download], "
                    "button:has-text('Download')")        # NOT yet confirmed — export flow pending

    _QUOTA = re.compile(r"(reached|out of|no more|limit).{0,40}"
                        r"(credit|generation|clip|quota|limit)", re.I)

    @staticmethod
    def _click_text(session, *texts, timeout=6000):
        """Real click on the smallest visible element whose text matches any of `texts`
        (Google SPAs swallow synthetic clicks less than TensorArt, but text-locate is the
        stable handle across redesigns). Tries exact match first, then substring so tile
        headings like 'Create AI videos' (card also holds a 'New' badge) still resolve.
        Returns True if clicked."""
        pg = session.page
        for t in texts:
            for pat in (re.compile(rf"^{re.escape(t)}$", re.I), re.compile(re.escape(t), re.I)):
                try:
                    loc = pg.get_by_text(pat).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=timeout)
                        return True
                except Exception:
                    pass
        return False

    def _quota_blocked(self, session) -> bool:
        try:
            return bool(self._QUOTA.search(session.page.inner_text("body")))
        except Exception:
            return False

    def submit(self, session, prompt: str):
        session.goto(self.home)
        time.sleep(6)
        self._guard(session)                     # vision: bail on login/captcha wall

        # CONFIRMED live flow (2026-09-23, profile googlevids__a1):
        # 1) open the "Let's start creating" dialog. The home '+' tile is a role=option
        #    template with no text/aria-label, so click the first template option.
        try:
            session.page.get_by_role("option").first.click(timeout=8000)
        except Exception:
            raise Wall("selector", "Vids '+' new-video tile not found — confirm live UI")
        time.sleep(4)
        # 2) aspect -> Portrait (button aria-label 'Create a portrait video'); sets 9:16
        #    for both canvas and the AI clip. get_by_text misses it (icon+label), role does.
        try:
            session.page.get_by_role("button", name=re.compile("Portrait", re.I)).first.click(timeout=6000)
        except Exception:
            raise Wall("selector", "Vids Portrait (9:16) toggle not found — confirm live UI")
        time.sleep(2)
        # 3) open the AI-video (Omni/Veo) generator tile -> editor with AI Video panel.
        if not self._click_text(session, "Create AI videos", "Create AI video"):
            raise Wall("selector", "Vids 'Create AI videos' tile not found — confirm live UI")
        time.sleep(7)

        if self._quota_blocked(session):
            raise Wall("rate_limit", "Google Vids monthly quota reached — refreshes on the 1st")

        # 4) prompt + fire. Real keyboard: Google SPAs ignore JS value injection.
        if not session.has(self.prompt_sel, timeout=8000):
            raise Wall("selector", "Vids AI-clip prompt box not found — confirm live UI")
        session.human_click(self.prompt_sel)
        session.page.keyboard.type(prompt, delay=18)
        time.sleep(1.5)
        # Generate arrow (aria-label='Generate') stays disabled until the prompt is non-empty.
        gen = session.page.locator(self.generate_sel).first
        if not gen.count() or not gen.is_enabled():
            raise Wall("selector", "Vids Generate not enabled — confirm prompt entry / live UI")
        gen.click()
        time.sleep(4)
        if self._quota_blocked(session):
            raise Wall("rate_limit", "Google Vids monthly quota reached — refreshes on the 1st")

    def poll(self, session) -> str:
        """CONFIRMED live: the AI-clip panel shows a NN% + Cancel while Veo renders; when
        done the result action bar (Insert / Extend / Edit / Recreate / Remove) appears.
        ~60s for a 10s clip. Quota message -> rate_limited so the runner cools it."""
        pg = session.page
        try:
            body = pg.inner_text("body")
        except Exception:
            body = ""
        if self._QUOTA.search(body):
            return "rate_limited"
        try:                                    # result bar up = render finished
            if (pg.get_by_role("button", name="Insert", exact=True).first.is_visible()
                    and pg.get_by_role("button", name="Recreate", exact=True).first.count()):
                return "done"
        except Exception:
            pass
        return "rendering"                      # keep looping (runner bounds via POLL_TIMEOUT)

    @staticmethod
    def _menu_box(pg, pat):
        """Center xy of the visible [role=menuitem] whose text matches `pat` (compiled).
        Google nested menus are flaky under locator hover; we locate by live box + click."""
        for o in pg.evaluate("""() => [...document.querySelectorAll('[role=menuitem]')].map(e=>{
                const r=e.getBoundingClientRect();
                return {t:(e.innerText||'').replace(/\\s+/g,' ').trim(),
                        x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), vis:r.width>0&&r.height>0};
              }).filter(o=>o.t && o.vis)"""):
            if pat.search(o["t"]):
                return o["x"], o["y"]
        return None

    def download(self, session, dest: str, timeout: float = 300.0) -> str:
        """CONFIRMED live: 1) Insert the finished clip into the scene, 2) File -> Download ►
        -> 'MP4 video (.mp4)'. The whole-vid export IS the single 9:16 clip (1080x1920 h264).
        Clicking the Download PARENT (not hover) opens the submenu reliably in headless."""
        pg = session.page
        # 1) add the rendered clip to the scene
        try:
            ins = pg.get_by_role("button", name="Insert", exact=True).first
            if ins.count() and ins.is_visible():
                ins.click(timeout=5000); time.sleep(5)
        except Exception:
            pass
        pg.keyboard.press("Escape"); time.sleep(0.5)          # drop any promo popup
        # 2) File menu -> Download submenu -> MP4
        pg.get_by_text(re.compile(r"^File$")).first.click(timeout=6000); time.sleep(1.2)
        dl = self._menu_box(pg, re.compile(r"^Download\b(?!.*\bas\b)", re.I))   # 'Download ►'
        if not dl:
            raise Wall("selector", "Vids File->Download item not found — confirm live UI")
        pg.mouse.click(*dl); time.sleep(1.8)                  # open submenu
        mp4 = self._menu_box(pg, re.compile(r"MP4", re.I))
        if not mp4:
            raise Wall("selector", "Vids 'MP4 video (.mp4)' export item not found")
        dls = []
        pg.on("download", lambda d: dls.append(d))
        pg.mouse.click(*mp4)
        deadline = time.time() + timeout
        while time.time() < deadline and not dls:
            time.sleep(1)
        if not dls:
            raise Wall("block", "Vids MP4 export did not deliver a download in time")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        dls[0].save_as(dest)
        return dest


ADAPTER = GoogleVids()
