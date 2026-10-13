"""
luma.py — Luma Dream Machine, on domain **luma.ai** (luma.ai/home; lumalabs.ai is only
marketing). Fast, clean quality. Auth = Supabase cookie `sb-...-auth-token` on luma.ai.

STATUS 2026-09-23 — WIRED + PROVEN, but the **free tier is a ONE-TIME TRIAL** for video.
The very first gen renders free (verified: a real 716x1284 9:16 3s h264 MP4 was generated
and downloaded through this adapter). Every gen AFTER that hard-paywalls: clicking Create
pops an "Upgrade Plan" modal instead of rendering (confirmed the gate is permanent, not a
cooldown). The "Create 333" credits shown are not spendable on video without a paid plan.
Per project policy (only a pure one-time/never-refreshing grant is disqualified) Luma free
is effectively OUT as a recurring source — but the adapter is fully wired and correct, so it
works as-is on a paid/upgraded account. submit() raises Wall("rate_limit") the moment the
upgrade modal appears, so free-tier runs fail cleanly instead of hanging.

LOGIN: open luma.ai in real Chrome, log in, then:
  import_login.py luma a1 --keyword luma --keyword lumalabs --keyword dream

FLOW (mapped live on profile luma__a1):
  1. luma.ai/home -> click the "Text to Video" tile -> composer (bottom bar).
  2. A one-time content-policy <dialog> gates the composer: TWO checkboxes must both be
     ticked before "Accept and continue" enables. Dismiss defensively (absent once accepted).
  3. Aspect chip (default "16:9") -> click -> pick "9:16". Chip flips to 9:16 (NOT remembered
     across sessions — set every run). Duration left at the cheapest "3s".
  4. Type the prompt in the textarea, click "Create <balance>" to fire. On free tier this pops
     the Upgrade Plan modal -> Wall("rate_limit"); on a paid account it renders.
  5. The clip renders server-side and lands in Assets (luma.ai/assets, newest-first). poll
     re-loads /assets each cycle and reads the newest tile's media URL (/api/media/<uuid>,
     from its <video> or poster <img>; /api/media/effect/ are template previews, skipped).
     A baseline captured at submit time distinguishes our new render from any older clip.
  6. DOWNLOAD: the UI Download button only opens a share menu (no file event); the robust
     path is an authenticated direct-fetch of the media URL via the session's request
     context (cookies ride along). Yields the 9:16 h264 MP4.

Session cookies are Supabase refresh-backed (not the fast-rotating Google kind), so no
per-run reimport hook; an expired session trips the login guard -> Wall("login").
"""

import os
import re
import time

from base import SiteAdapter, Wall

_HOME = "https://luma.ai/home"
_ASSETS = "https://luma.ai/assets"


class Luma(SiteAdapter):
    name = "luma"
    home = _HOME
    credit_cost = 1           # advisory; free pool ~333 credits/month, per-gen cost small
    aspect = "9:16"

    _QUOTA = re.compile(r"(out of|no more|ran out|not enough|insufficient).{0,30}"
                        r"(credit|generation)|upgrade to (continue|create)", re.I)
    # free tier hard-paywalls video after a single trial gen: clicking Create pops this modal.
    _PAYWALL = re.compile(r"Upgrade Plan|Unlock the full power of Luma", re.I)

    def _paywalled(self, session) -> bool:
        try:
            return bool(self._PAYWALL.search(session.page.inner_text("body")))
        except Exception:
            return False

    @staticmethod
    def _box(pg, pat, mx=26):
        """Center xy + text of the first small visible element whose text matches `pat`.
        Text-locate survives Luma's class-name churn; we click live bbox centers because the
        bottom-bar pills and dialog buttons are composite nodes."""
        for o in pg.evaluate(
            """()=>[...document.querySelectorAll('button,[role=button],a,span,div,li')].map(e=>{
                const r=e.getBoundingClientRect();
                return {t:(e.textContent||'').replace(/\\s+/g,' ').trim(),
                        x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),
                        w:Math.round(r.width),h:Math.round(r.height)};})
              .filter(o=>o.w>10&&o.w<340&&o.h>10&&o.h<70)"""):
            if re.search(pat, o["t"]) and len(o["t"]) < mx:
                return o
        return None

    @staticmethod
    def _newest_media(pg):
        """Base media URL (query stripped) of the newest real result on the page — a mounted
        <video> OR its poster <img> (Luma lazy-mounts <video>, so the poster is the reliable
        signal). Skips /api/media/effect/ template previews. The newest asset tile is first in
        DOM order, so a rendering tile (spinner, no media yet) leaves the FIRST media element
        pointing at the previous clip — which the baseline check treats as 'still rendering'.
        Returns None when no result media is on the page."""
        return pg.evaluate(r"""()=>{
            const isres = s => s && /\/api\/media\/(?!effect\/)/.test(s) && !s.startsWith('blob:');
            for (const el of document.querySelectorAll('video,img')) {
                const s = el.currentSrc || el.src || '';
                if (isres(s)) return s.split('?')[0];
            }
            return null;
        }""")

    def _quota_blocked(self, session) -> bool:
        try:
            return bool(self._QUOTA.search(session.page.inner_text("body")))
        except Exception:
            return False

    def submit(self, session, prompt: str):
        pg = session.page
        session.goto(self.home)
        time.sleep(10)
        # login guard: signed-out luma.ai has no composer entry (same locator we click with).
        t2v = pg.get_by_text(re.compile("Text to Video", re.I)).first
        if not t2v.count():
            self._guard(session)                      # vision may name captcha/login
            raise Wall("login", "luma composer absent (signed out?) — re-import cookies")
        t2v.click()
        time.sleep(5)

        # one-time content-policy gate: tick BOTH checkboxes, then "Accept and continue".
        if pg.locator("dialog[open]").count():
            cbs = pg.locator("dialog[open] input[type=checkbox]")
            for i in range(cbs.count()):
                try:
                    cbs.nth(i).check(timeout=2000)
                except Exception:
                    pass
            acc = self._box(pg, r"Accept and continue")
            if acc:
                pg.mouse.click(acc["x"], acc["y"])
            time.sleep(2)

        # aspect -> 9:16 (skip if already set). Default chip reads "16:9".
        if not self._box(pg, r"9:16"):
            chip = self._box(pg, r"^16:9$") or {"x": 750, "y": 859}
            pg.mouse.click(chip["x"], chip["y"]); time.sleep(2)
            opt = self._box(pg, r"^9:16$")
            if not opt:
                raise Wall("selector", "luma 9:16 option not found — confirm live UI")
            pg.mouse.click(opt["x"], opt["y"]); time.sleep(1.5)
            pg.keyboard.press("Escape"); time.sleep(1)
        if not self._box(pg, r"9:16"):
            raise Wall("selector", "luma aspect did not flip to 9:16 — confirm live UI")

        if self._quota_blocked(session):
            raise Wall("credits", "luma monthly credits spent — refreshes monthly",
                               refill="monthly")

        ta = pg.locator("textarea").first
        if not ta.count():
            raise Wall("selector", "luma prompt textarea not found — confirm live UI")
        ta.click()
        pg.keyboard.type(prompt, delay=12); time.sleep(1)

        create = self._box(pg, r"Create \d", mx=20)   # button uniquely reads "Create <balance>"
        if not create:
            raise Wall("selector", "luma Create button not found — confirm live UI")
        # baseline the current newest result BEFORE firing, so poll can tell our new render
        # apart from any older clip already sitting at the top of the Assets grid.
        session._luma_baseline = self._newest_media(pg)
        pg.mouse.click(create["x"], create["y"]); time.sleep(5)
        if self._paywalled(session):
            raise Wall("credits", "luma free tier paywalled — Create opens the Upgrade "
                                     "Plan modal (free video is a one-time trial only)")
        if self._quota_blocked(session):
            raise Wall("credits", "luma monthly credits spent — refreshes monthly",
                               refill="monthly")

    def poll(self, session) -> str:
        """Done when a NEW (non-effect) result — different from the pre-submit newest — appears
        at the top of the grid. Re-navigating /assets each cycle gives a fresh, newest-first DOM
        (the composer's inline row can stall its live update in headless). Baseline guards
        against 'done' on the stale prior clip."""
        pg = session.page
        if self._quota_blocked(session):
            return "rate_limited"
        if "/assets" not in pg.url:
            session.goto(_ASSETS); time.sleep(6)
        else:
            pg.reload(wait_until="commit"); time.sleep(6)
        cur = self._newest_media(pg)
        baseline = getattr(session, "_luma_baseline", None)
        return "done" if (cur and cur != baseline) else "rendering"

    def download(self, session, dest: str) -> str:
        """Authenticated direct-fetch of the newest result's media URL (the UI Download button
        only opens a share menu). Cookies ride the session request context."""
        pg = session.page
        if "/assets" not in pg.url:
            session.goto(_ASSETS); time.sleep(8)
        url = self._newest_media(pg)
        if not url:
            raise Wall("block", "luma finished clip not found to download")
        r = pg.request.get(url)
        if not r.ok:
            raise Wall("block", f"luma media fetch failed HTTP {r.status}")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(r.body())
        return dest


ADAPTER = Luma()
