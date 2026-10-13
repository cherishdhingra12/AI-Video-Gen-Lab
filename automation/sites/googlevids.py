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
so the runner cools it (ideally until the 1st; see learn.py note in memory).

DOWNLOAD (fixed 2026-09-26): the finished Veo clip is served from *.usercontent.google.com
(?...&filename=video.mp4) — 720x1280 9:16 h264 WITH audio. We capture that URL off the wire
(like Krea/Leonardo) and fetch it directly. This replaced the old File->Download->MP4 project
export, which renders the whole vid server-side (1080x1920) but flaked on the download event.
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

    _QUOTA = re.compile(r"(reached|out of|no more|limit).{0,40}"
                        r"(credit|generation|clip|quota|limit)", re.I)

    # ---- passive capture of the rendered clip URL (gstatic.com/docs/... are promo decoys) ----
    def _watch(self, session):
        # Reset + rebind EVERY render: the ADAPTER is a singleton reused across scenes (runner
        # loops, render_*.py batches), and each scene is a fresh Session/page. A one-time guard
        # here left later scenes with no listeners and a stale _mp4 → download() re-saved the
        # PREVIOUS scene's clip (byte-identical dupes). Clear per submit so each scene captures
        # only its own finished clip.
        self._mp4 = []

        def note(u):
            u = (u or "").split("#")[0]
            if "usercontent.google.com" in u and ("video.mp4" in u or "/download?" in u):
                self._mp4.append(u)

        session.page.on("response", lambda r: note(r.url))
        session.page.on("request", lambda q: note(q.url))


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
        self._watch(session)
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
            raise Wall("credits", "Google Vids monthly quota reached — refreshes on the 1st",
                       refill="monthly")

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
            raise Wall("credits", "Google Vids monthly quota reached — refreshes on the 1st",
                       refill="monthly")

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

    def download(self, session, dest: str) -> str:
        """Fetch the finished Veo clip straight off the wire (720x1280 9:16 h264+audio),
        the URL passively captured by _watch during the render. Robust vs. the old
        File->Download->MP4 project export (which flaked on the download event)."""
        pg = session.page
        url = next(iter(reversed(getattr(self, "_mp4", []))), None)
        if not url:                              # fallback: the result-panel <video> src
            vids = pg.evaluate("""() => {const o=[];document.querySelectorAll('video').forEach(v=>{
              if(v.currentSrc)o.push(v.currentSrc);if(v.src)o.push(v.src);});return o;}""")
            url = next((v for v in vids if "usercontent.google.com" in v), None)
        if not url:
            raise Wall("selector", "no finished Vids clip URL on the wire — confirm live UI")
        resp = pg.request.get(url)               # runs in-context, carries session cookies
        if resp.status != 200:
            raise Wall("block", f"Vids clip download HTTP {resp.status}")
        body = resp.body()
        if len(body) < 100_000:
            raise Wall("block", f"Vids clip only {len(body)}B — not a real render")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(body)
        return dest


ADAPTER = GoogleVids()


if __name__ == "__main__":  # self-check: capture state must reset + rebind every render
    class _FakePage:
        def __init__(self): self.bound = 0
        def on(self, *_a, **_k): self.bound += 1
    class _FakeSession:
        def __init__(self): self.page = _FakePage()

    a = GoogleVids()
    s1 = _FakeSession(); a._watch(s1); a._mp4.append("stale-from-scene-1")
    s2 = _FakeSession(); a._watch(s2)                 # next scene, fresh page
    assert a._mp4 == [], "download would re-save the previous scene's clip"
    assert s2.page.bound == 2, "listeners not rebound to the new scene's page"
    print("googlevids _watch reset ok")

