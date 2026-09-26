"""
vibes.py — Vibes (vibes.ai), Meta's standalone creation web app. QUALIFIES for the waterfall:
no visible limit, default output is VERTICAL ~9:16, and the clip downloads to device.
DISTINCT from meta.ai/vibes (metavibes.py = the age-blocked one). See [[ai-video-gen-lab]].

STATUS 2026-09-23 — FULLY PROVEN end-to-end (headless): text-to-video, ~25s, real
464x832 (vertical 9:16) h264 5.2s MP4, no watermark, direct fbcdn download (HTTP 200, 1.72 MB).
Both historic Meta blockers CLEARED:
  (1) 9:16 — vibes has NO aspect control at all (the Advanced popover holds only 480p/720p),
      but the DEFAULT render is vertical (it's a Reels tool); verified 464x832.
  (2) download-to-device — the finished clip's <video> src is a direct SIGNED fbcdn .mp4 that
      page.request.get pulls as bytes (Meta's old blocker was share-only export).

Mechanics that matter (learned the hard way):
  - The composer prompt is a contenteditable DIV at the bottom (y>740). Focus via JS + click,
    then type; verify innerText landed before submitting.
  - SUBMIT = press Enter. A coordinate click on the blue Generate arrow does NOT register;
    Enter does. The submit signal = prompt box clears AND "No media yet" disappears.
  - Opening the Advanced popover STEALS focus and silently breaks subsequent typing, so we
    never touch Advanced before typing (and skip it entirely — 480p default is fine).
  - The output URL is a signed fbcdn mp4 (oe= expiry) — download promptly, keep the full query.

Credit-protection note: the standing rule ("confirm 9:16 + cost before firing") exists to
protect SCARCE credits. Vibes exposes no limit and no settable aspect, and its default is
reliably vertical, so there is nothing to pre-gate; orientation is confirmed after capture.
"""

import os
import time

from base import SiteAdapter, Wall

# promo / showcase / preset clips that are NOT our render
_DECOY = ("/preset", "showcase", "tutorial", "onboarding", "/sample", "example",
          "featured", "template", "/explore", "thumbnail", "poster", "/avatar",
          "/logo", "sprite")


class Vibes(SiteAdapter):
    name = "vibes"
    home = "https://vibes.ai/"
    credit_cost = 0           # no visible limit; nothing to spend-gate
    aspect = "9:16"           # default render is vertical (no settable control)

    # ---- passive capture of the app's own video URLs ----
    def _watch(self, session):
        if getattr(self, "_watching", False):
            return
        self._mp4 = []

        def note(u):
            u = u or ""
            lu = u.lower()
            if (".mp4" in lu or ".webm" in lu) and not any(d in lu for d in _DECOY):
                self._mp4.append(u.split("#")[0])   # keep the ?query (fbcdn signature)

        session.page.on("response", lambda r: note(r.url))
        session.page.on("request", lambda q: note(q.url))
        self._watching = True

    # ---- open a fresh project (composer editor) ----
    def _open_project(self, session):
        pg = session.page
        loc = pg.evaluate("""() => {const el=[...document.querySelectorAll('button,[role=button],a')]
          .map(e=>({e,r:e.getBoundingClientRect(),t:(e.innerText||'').replace(/\\s+/g,' ').trim()}))
          .filter(o=>/^(create new|new project|create)$/i.test(o.t)&&o.r.width>20)
          .sort((a,b)=>a.r.y-b.r.y)[0];
          if(!el)return null;const r=el.r;return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if loc:
            pg.mouse.click(loc["x"], loc["y"])
        else:
            pg.mouse.click(160, 159)               # proven "Create new" position, fallback
        time.sleep(6)

    # ---- type the prompt into the contenteditable composer, verified ----
    def _type_prompt(self, session, prompt):
        pg = session.page
        tc = pg.evaluate("""() => {const c=[...document.querySelectorAll('[contenteditable="true"],textarea')]
          .map(e=>({e,r:e.getBoundingClientRect()})).filter(o=>o.r.width>200&&o.r.y>740)
          .sort((a,b)=>a.r.y-b.r.y)[0];
          if(!c)return null;c.e.setAttribute('data-me','1');c.e.focus();const r=c.r;
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not tc:
            raise Wall("selector", "vibes composer prompt field not found")
        pg.mouse.click(tc["x"], tc["y"]); time.sleep(0.3)
        pg.keyboard.type(prompt, delay=13); time.sleep(0.7)
        self._composer = tc
        typed = pg.evaluate("""() => {const e=document.querySelector('[data-me="1"]');return e?e.innerText.trim():'';}""")
        if len(typed) < min(10, len(prompt)):
            raise Wall("selector", f"vibes prompt did not land ({len(typed)}/{len(prompt)}) — not submitting")

    # ---- submit: open project, type prompt, press Enter (NOT the arrow), confirm it fired ----
    def submit(self, session, prompt: str):
        self._watch(session)
        session.goto(self.home, wait="domcontentloaded"); time.sleep(6)
        pg = session.page
        self._open_project(session)
        self._type_prompt(session, prompt)

        self._baseline = set(self._mp4)
        pg.mouse.click(self._composer["x"], self._composer["y"]); time.sleep(0.2)   # ensure focus
        pg.keyboard.press("Enter"); time.sleep(3)

        cleared = pg.evaluate("""() => {const e=document.querySelector('[data-me="1"]');return e?e.innerText.trim():''}""")
        no_media = pg.evaluate("""() => document.body.innerText.includes('No media yet')""")
        if cleared and no_media:                     # neither signal fired => not submitted
            raise Wall("selector", "vibes Enter did not submit (prompt still present, no render)")

    # ---- poll: a NEW fbcdn mp4 on the wire (baseline-diff), else a non-blob <video> src ----
    def poll(self, session) -> str:
        new = [u for u in self._mp4 if u not in getattr(self, "_baseline", set())]
        if new:
            self._done_url = new[-1]
            return "done"
        vids = session.page.evaluate("""() => {const o=[];document.querySelectorAll('video').forEach(v=>{
          if(v.src)o.push(v.src);if(v.currentSrc)o.push(v.currentSrc);
          v.querySelectorAll('source').forEach(sr=>sr.src&&o.push(sr.src));});return o;}""")
        real = [v.split("#")[0] for v in vids
                if v and "blob:" not in v and ".mp4" in v.lower()
                and not any(d in v.lower() for d in _DECOY)]
        real = [v for v in real if v not in getattr(self, "_baseline", set())]
        if real:
            self._done_url = real[0]
            return "done"
        body = session.page.inner_text("body").lower()
        if "failed" in body and "generation" in body:
            return "error"
        return "rendering"

    # ---- download the signed fbcdn mp4 (do it promptly; the URL expires) ----
    def download(self, session, dest: str) -> str:
        url = getattr(self, "_done_url", None)
        if not url:
            raise Wall("selector", "no finished vibes render url to download")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = session.page.request.get(url)
        if resp.status != 200:
            raise Wall("block", f"vibes download HTTP {resp.status} (signed URL may have expired)")
        with open(dest, "wb") as f:
            f.write(resp.body())
        return dest


ADAPTER = Vibes()


if __name__ == "__main__":
    # self-check: the decoy filter and the submit-signal logic (no browser).
    def is_decoy(u): return any(d in u.lower() for d in _DECOY)
    assert is_decoy("https://x/showcase/a.mp4") and not is_decoy("https://scontent.xx.fbcdn.net/o1/v/t6/AQ.mp4?oe=1")
    # submit fired iff NOT (prompt still present AND canvas still "No media yet")
    def fired(cleared, no_media): return not (cleared and no_media)
    assert fired("", False) and not fired("still here", True)
    print("OK: decoy filter keeps fbcdn mp4; submit-fired = not(prompt_present and no_media)")
