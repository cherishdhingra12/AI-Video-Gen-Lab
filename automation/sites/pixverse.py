"""
pixverse.py — PixVerse (app.pixverse.ai). DAILY-refresh CONFIRMED and full pipeline
PROVEN live 2026-09-22 (submit spent exactly 38 credits; a 576x1024 9:16 clip whose
prompt matched was polled + downloaded).

Daily credits: "Daily Login 30 Credits" claimed daily AND they accumulate day to day
(saw 52 -> 14 after one 38-credit gen), so ~0.8 gens/day sustainable at 540P — thin
but genuinely recurring, unlike Hailuo's one-time grant.

Proven mechanics (why each line is what it is):
  - create URL = /creation/video
  - prompt box: page has several HIDDEN textareas; the real one is matched by its
    placeholder, else human_type grabs the first (invisible) one and times out.
  - aspect: a composer pill "540P 16:9 5s"; click it, pick 9:16, pill flips -> confirmed.
  - generate: button reads "Create 38". get_by_role/has-text landed on a WRAPPER and
    silently no-op'd (balance never moved). A mouse click on the exact element's
    CENTER fires the job (balance dropped 38). So we click by bounding-box centre.
  - list/poll: the personal generations are returned by a POST to
    /creative_platform/asset/library/list (NOT content/relation/list — that's the
    public community feed). It's not GET-able (404) and fires on composer load, so we
    trigger it with a navigation and read it via expect_response. Newest item's
    video_status==1 means done; item["url"] is the mp4.
  - download: item["url"] fetched with page.request.get (shares cookies, streams —
    a JS-array fetch OOM'd the driver on large files).
"""

import os
import time
import json

from base import SiteAdapter, Wall

_LIB = "asset/library/list"          # personal generations endpoint (POST, fires on load)


class PixVerse(SiteAdapter):
    name = "pixverse"
    home = "https://app.pixverse.ai/creation/video"
    credit_cost = 38                         # "Create 38" on the button, confirmed

    # page has several hidden textareas; target the visible composer box by placeholder
    prompt_sel = "textarea[placeholder*='Describe the content']"
    aspect = "9:16"

    # ---- aspect pill ----
    def _pill(self, session):
        return session.page.evaluate_handle("""() => {
          const ok=[...document.querySelectorAll('*')].filter(e=>{
            const t=(e.innerText||'').replace(/\\s+/g,' ').trim();
            return /(16:9|9:16|1:1)/.test(t)&&/(360P|540P|720P|1080P)/i.test(t)&&t.length<26;});
          ok.sort((a,b)=>a.querySelectorAll('*').length-b.querySelectorAll('*').length);
          return ok[0]||null;}""").as_element()

    def _pill_text(self, session) -> str:
        el = self._pill(session)
        try:
            return (el.inner_text() or "").replace("\n", " ") if el else ""
        except Exception:
            return ""

    def _click_text(self, session, text, timeout=2500) -> bool:
        try:
            el = session.page.get_by_text(text, exact=True)
            if el.count():
                el.first.click(timeout=timeout)
                return True
        except Exception:
            pass
        return False

    def configure(self, session):
        """Open the pill popover, click 9:16, confirm or raise (protect credits)."""
        pill = self._pill(session)
        if pill:
            pill.click(timeout=3000)
            time.sleep(1.0)
            self._click_text(session, self.aspect)
            session.page.keyboard.press("Escape")
            time.sleep(0.6)
        txt = self._pill_text(session)
        if self.aspect not in txt.replace(" ", ""):
            raise Wall("selector", f"could not confirm {self.aspect} (pill='{txt}')")

    # ---- personal generations list (the poll target) ----
    def _library(self, session, navigate=True):
        """Trigger asset/library/list via a composer navigation and return its items
        newest-first. The endpoint is POST + fires on load, so expect_response is the
        only reliable read (direct GET 404s)."""
        pg = session.page
        try:
            with pg.expect_response(lambda r: _LIB in (r.url or ""), timeout=30000) as info:
                if navigate:
                    session.goto(self.home)
                else:
                    pg.reload(wait_until="commit")
            data = info.value.json().get("Resp", {}).get("data") or []
        except Exception:
            return []
        data.sort(key=lambda it: str(it.get("video_created_at") or it.get("created_at") or ""),
                  reverse=True)
        return data

    @staticmethod
    def _ids(items):
        return {it.get("asset_id") or it.get("video_id") for it in items}

    # ---- submit ----
    def submit(self, session, prompt: str):
        # navigating here also fires library/list -> record the baseline ids so poll
        # knows which item is the NEW one (not an older render).
        self._base_ids = self._ids(self._library(session, navigate=True))
        time.sleep(2)
        self._guard(session)
        self.configure(session)
        session.human_type(self.prompt_sel, prompt)

        # click the EXACT "Create N" button element's centre (a text/role click hits a
        # wrapper and no-ops). Fail loud if the button isn't found — never silently skip.
        info = session.page.evaluate("""() => {
          const b=[...document.querySelectorAll('button,[role=button]')]
            .find(e=>/^Create( \\d+)?$/.test((e.innerText||'').replace(/\\s+/g,' ').trim()));
          if(!b) return null; const r=b.getBoundingClientRect();
          return {x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2), dis:b.disabled};}""")
        if not info:
            raise Wall("selector", "Create button not found")
        if info.get("dis"):
            raise Wall("selector", "Create button disabled (prompt/config missing?)")
        session.page.mouse.click(info["x"], info["y"])
        time.sleep(3)

    # ---- poll ----
    def poll(self, session) -> str:
        items = self._library(session, navigate=True)
        base = getattr(self, "_base_ids", set())
        # the newest item whose id wasn't present at submit time is our job
        new = [it for it in items if (it.get("asset_id") or it.get("video_id")) not in base]
        if not new:
            # nothing new yet; surface an explicit credit wall if the UI shows one
            try:
                body = session.page.inner_text("body")[:4000].lower()
                if any(k in body for k in ("insufficient", "not enough", "run out",
                                           "no credit", "quota", "daily limit")):
                    raise Wall("rate_limit", "insufficient credits")
            except Wall:
                raise
            except Exception:
                pass
            return "rendering"
        it = new[0]
        if it.get("video_status") == 1 and (it.get("url") or it.get("video_url")):
            self._done_url = it.get("url") or it.get("video_url")
            return "done"
        return "rendering"

    # ---- download ----
    def download(self, session, dest: str) -> str:
        url = getattr(self, "_done_url", None)
        if not url:                       # poll didn't stash one; re-read the list
            items = self._library(session, navigate=True)
            base = getattr(self, "_base_ids", set())
            for it in items:
                if (it.get("asset_id") or it.get("video_id")) not in base and it.get("video_status") == 1:
                    url = it.get("url") or it.get("video_url")
                    break
        if not url:
            raise Wall("selector", "no finished PixVerse render url to download")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = session.page.request.get(url.split("#")[0])   # streams to disk, shares cookies
        if resp.status != 200:
            raise Wall("block", f"download HTTP {resp.status}")
        with open(dest, "wb") as f:
            f.write(resp.body())
        return dest


ADAPTER = PixVerse()
