"""
krea.py — Krea (krea.ai/video), LTX-2 model. QUALIFIES as a DAILY-refresh 9:16 source.

STATUS 2026-09-23 — the old "9:16 unsettable / 578cu" verdict was WRONG (it was measured on
MiniMax H3 with the aspect Radix popover that wouldn't open headless). Re-probed with the
**LTX-2** model the user asked for:
  - free workspace balance = ~100 credits/DAY (account menu reads "N Credits remaining";
    daily reset). LTX-2 19B costs **35** ⇒ ~2 clips/day.  (2026-09-26: card relabeled
    "LTX-2" → "LTX-2 19B", cost 34 → 35.)
  - LTX-2 is free-accessible (LTX-2.5 Pro is Pro-gated; MiniMax H3=578, others >100/day).
  - 9:16 IS settable: the aspect control is an ICON (not a "16:9" text pill). Click it →
    "Aspect Ratio" menu with Landscape / Portrait tiles. Picking Portrait flips the composer
    icon's inner <svg rect> from 15x9 (wide) to 9x15 (tall) — that flip is our POSITIVE gate.
  - res fixed 512p, 5s, audio on. 512p 9:16 ≈ 512x912.

Flow (all proven live, headless): open modal (model chip ~395,558) → search "LTX-2" →
click the card body (title y-110) → LTX-2 selected → open aspect icon (~829,513), click the
Portrait tile, confirm rect h>w → type prompt in the composer textarea → Generate arrow
(~1185,513). Output captured passively off the wire (mp4), like Leonardo. See [[ai-video-gen-lab]].
"""

import os
import re
import time

from base import SiteAdapter, Wall

# promo / showcase / preset clips that are NOT our render
_DECOY = ("/preset", "showcase", "tutorial", "onboarding", "/sample", "example",
          "featured", "template", "/explore", "thumbnail", "poster")


class Krea(SiteAdapter):
    name = "krea"
    home = "https://www.krea.ai/video"
    credit_cost = 35          # LTX-2 19B; free daily bucket ~100 ⇒ ~2 gens/day
    aspect = "9:16"
    daily_cap = 100           # free credits/day (daily reset)

    # ---- passive capture of the app's own mp4 URLs ----
    def _watch(self, session):
        if getattr(self, "_watching", False):
            return
        self._mp4 = []

        def note(u):
            u = u or ""
            if ".mp4" in u.lower() and not any(d in u.lower() for d in _DECOY):
                self._mp4.append(u.split("#")[0])

        session.page.on("response", lambda r: note(r.url))
        session.page.on("request", lambda q: note(q.url))
        self._watching = True

    # ---- perception helpers ----
    @staticmethod
    def _balance(pg):
        """Free wallet: account chip (bottom-left) → 'N Credits remaining'."""
        pg.mouse.click(34, 868); time.sleep(1.4)
        body = pg.inner_text("body")
        pg.keyboard.press("Escape"); time.sleep(0.5)
        m = re.search(r'(\d+)\s*Credits?\s*remaining', body, re.I)
        return int(m.group(1)) if m else None

    @staticmethod
    def _aspect_icon(pg):
        """The no-text composer button between the duration chip and the cost; its inner
        <svg rect> encodes orientation (w>h landscape, h>w portrait)."""
        return pg.evaluate("""() => {const b=[...document.querySelectorAll('button,[role=button]')]
          .map(e=>({e,r:e.getBoundingClientRect(),t:(e.innerText||'').trim()}))
          .filter(o=>o.r.y>485&&o.r.y<575&&o.r.x>780&&o.r.x<1000&&o.t===''&&o.r.width>16&&o.r.width<70)
          .sort((a,b)=>a.r.x-b.r.x)[0];
          if(!b)return null;const R=b.e.getBoundingClientRect();
          const g={x:Math.round(R.x+R.width/2),y:Math.round(R.y+R.height/2)};
          const rc=b.e.querySelector('svg rect');
          if(rc){const r=rc.getBoundingClientRect();g.w=Math.round(r.width);g.h=Math.round(r.height);}
          return g;}""")

    @staticmethod
    def _cost(pg):
        return pg.evaluate("""() => {const el=[...document.querySelectorAll('button,[role=button],div,span')]
          .find(e=>{const r=e.getBoundingClientRect();const t=(e.innerText||'').replace(/\\s+/g,' ').trim();
            return r.y>490&&r.y<540&&r.x>1080&&r.x<1175&&/^[\\d ]{1,6}$/.test(t);});
          return el?parseInt(el.innerText.replace(/\\s+/g,''),10):null;}""")

    # ---- select LTX-2 19B via the model modal ----
    def _pick_ltx2(self, session):
        pg = session.page
        # 2026-09-26: the model chip moved from the composer (~395,558) to the TOP header.
        # Locate it by text (a 'Model <name>' control near the top) instead of fixed coords.
        chip = pg.evaluate("""() => {const c=[...document.querySelectorAll('button,[role=button],div,span')]
          .map(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();return {e,t,r};})
          .filter(o=>o.r.y<80&&o.r.x<700&&o.r.width>60&&o.r.width<320&&o.r.height>14&&o.r.height<44
                     &&/(MiniMax|LTX|Wan|Veo|Hailuo|Kling|Seedance|Turbo|Fast|Pro|\\dB)/i.test(o.t))
          .sort((a,b)=>b.r.width-a.r.width)[0];
          if(!c)return null;const r=c.e.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not chip:
            raise Wall("selector", "model chip not found in top header — confirm live UI")
        pg.mouse.click(chip["x"], chip["y"]); time.sleep(1.8)   # open model modal
        # the modal lists every model; find the LTX-2 19B card directly (no blind search box).
        loc = pg.evaluate("""() => {const el=[...document.querySelectorAll('*')]
          .filter(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
            return t==='LTX-2 19B'&&r.width>40&&r.width<420&&r.height>12&&r.height<70;})
          .sort((a,b)=>a.getBoundingClientRect().width-b.getBoundingClientRect().width)[0];
          if(!el)return null;el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not loc:
            raise Wall("selector", "LTX-2 19B card not found in model modal")
        time.sleep(0.6)
        pg.mouse.click(loc["x"], loc["y"]); time.sleep(2.5)   # click the card
        hdr = pg.evaluate("""() => {const b=[...document.querySelectorAll('*')]
          .find(e=>/^Model\\s+LTX-2 19B/i.test((e.innerText||'').replace(/\\s+/g,' ').trim()));
          return !!b;}""")
        if not hdr:
            raise Wall("selector", "LTX-2 19B not selected (no 'Model LTX-2 19B' header)")

    # ---- set 9:16 via the aspect icon; POSITIVE gate = composer icon rect flips to h>w ----
    def _set_portrait(self, session):
        pg = session.page
        ic = self._aspect_icon(pg)
        if not ic:
            raise Wall("selector", "aspect icon not found on LTX-2 composer")
        for _ in range(4):
            pg.mouse.click(ic["x"], ic["y"]); time.sleep(1.4)     # open Aspect Ratio menu
            tile = pg.evaluate("""() => {const el=[...document.querySelectorAll('*')]
              .find(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
                return t==='Portrait'&&r.width>40&&r.width<170&&r.height>40&&r.height<170;});
              if(!el)return null;const r=el.getBoundingClientRect();
              return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
            if tile:
                pg.mouse.click(tile["x"], tile["y"]); time.sleep(1.2)
                break
            pg.keyboard.press("Escape"); time.sleep(0.5)
        after = self._aspect_icon(pg)
        if not (after and after.get("h") and after.get("w") and after["h"] > after["w"]):
            raise Wall("selector", f"portrait not confirmed (icon rect={after}) — refusing to spend")

    # ---- type the prompt into the composer textarea/contenteditable ----
    def _type_prompt(self, session, prompt):
        pg = session.page
        tc = pg.evaluate("""() => {const c=[...document.querySelectorAll('textarea,[contenteditable=\"true\"]')]
          .map(e=>({e,r:e.getBoundingClientRect()}))
          .filter(o=>o.r.width>250&&o.r.y>430&&o.r.y<500).sort((a,b)=>a.r.y-b.r.y)[0];
          if(!c)return null;c.e.focus();c.e.setAttribute('data-me','1');
          return{x:Math.round(c.r.x+c.r.width/2),y:Math.round(c.r.y+c.r.height/2)};}""")
        if not tc:
            raise Wall("selector", "composer prompt field not found")
        pg.mouse.click(tc["x"], tc["y"]); time.sleep(0.3)
        pg.keyboard.type(prompt, delay=16); time.sleep(0.8)
        typed = pg.evaluate("""() => {const e=document.querySelector('[data-me=\"1\"]');
          return e?(e.value!==undefined?e.value:e.innerText):'';}""") or ""
        if len(typed.strip()) < len(prompt) - 2:
            raise Wall("selector", f"prompt did not fully land ({len(typed)}/{len(prompt)}) — not spending")

    # ---- submit: LTX-2, balance gate, portrait gate, cost gate, prompt, Generate ----
    def submit(self, session, prompt: str):
        self._watch(session)
        self._prompt = prompt
        session.goto(self.home, wait="domcontentloaded"); time.sleep(9)
        pg = session.page
        for _ in range(2):
            pg.keyboard.press("Escape"); time.sleep(0.4)

        self._pick_ltx2(session)

        bal = self._balance(pg)
        if bal is not None and bal < self.credit_cost:
            raise Wall("credits", f"only {bal} credits < {self.credit_cost} needed — refreshes daily",
                               refill="daily")

        self._set_portrait(session)

        cost = self._cost(pg) or self.credit_cost
        if cost > self.credit_cost + 5:
            raise Wall("selector", f"unexpected cost {cost} (expected ~{self.credit_cost}) — not spending")
        if bal is not None and cost > bal:
            raise Wall("rate_limit", f"cost {cost} > balance {bal} — refreshes daily")

        self._type_prompt(session, prompt)

        self._baseline = set(self._mp4)
        btn = pg.evaluate("""() => {const g=e=>{const r=e.getBoundingClientRect();
            return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};};
          // 1) button whose aria-label/title says Generate
          let b=[...document.querySelectorAll('button,[role=button]')]
            .find(e=>/generate/i.test((e.getAttribute('aria-label')||e.getAttribute('title')||'')));
          if(b)return g(b);
          // 2) rightmost small no-text icon button on the composer row
          b=[...document.querySelectorAll('button,[role=button]')]
            .map(e=>({e,r:e.getBoundingClientRect(),t:(e.innerText||'').trim()}))
            .filter(o=>o.r.y>485&&o.r.y<575&&o.r.x>1160&&o.t===''&&o.r.width>20&&o.r.width<70)
            .sort((a,b)=>b.r.x-a.r.x)[0];
          return b?g(b.e):null;}""")
        if not btn:
            raise Wall("selector", "Generate arrow not found")
        pg.mouse.click(btn["x"], btn["y"]); time.sleep(5)

    # ---- poll: watch the wire for a NEW mp4 (baseline-diff), else scan <video> ----
    def poll(self, session) -> str:
        new = [u for u in self._mp4 if u not in getattr(self, "_baseline", set())]
        if new:
            self._done_url = new[-1]
            return "done"
        vids = session.page.evaluate("""() => {const o=[];document.querySelectorAll('video').forEach(v=>{
          if(v.src)o.push(v.src);if(v.currentSrc)o.push(v.currentSrc);
          v.querySelectorAll('source').forEach(sr=>sr.src&&o.push(sr.src));});return o;}""")
        real = [v.split("#")[0] for v in vids
                if ".mp4" in v.lower() and not any(d in v.lower() for d in _DECOY)]
        real = [v for v in real if v not in getattr(self, "_baseline", set())]
        if real:
            self._done_url = real[0]
            return "done"
        body = session.page.inner_text("body").lower()
        if "upgrade" in body and "credit" in body:
            return "rate_limited"
        return "rendering"

    # ---- download the captured mp4 ----
    def download(self, session, dest: str) -> str:
        url = getattr(self, "_done_url", None)
        if not url:
            raise Wall("selector", "no finished Krea render url to download")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = session.page.request.get(url)
        if resp.status != 200:
            raise Wall("block", f"download HTTP {resp.status}")
        with open(dest, "wb") as f:
            f.write(resp.body())
        return dest


ADAPTER = Krea()


if __name__ == "__main__":
    # self-check: cost/balance parsing and the portrait gate rule (h>w) — no browser.
    assert int(re.search(r'(\d+)\s*Credits?\s*remaining', "99 Credits remaining").group(1)) == 99
    def portrait_ok(w, h): return bool(h and w and h > w)
    assert portrait_ok(9, 15) and not portrait_ok(15, 9)
    print("OK: balance parse=99, portrait gate = icon rect h>w only")
