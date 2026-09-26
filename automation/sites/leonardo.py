"""
leonardo.py — Leonardo.ai (app.leonardo.ai), video mode, MiniMax H3 model.

QUALIFIES (validated live 2026-09-23): free = 150 tokens/DAY (daily reset, no accrual).
MiniMax H3 costs 90 tokens ⇒ ONE 9:16 HD clip/day (a 2nd 90 exceeds 150). Proven end to
end: balance 150→60 after one gen, output downloaded HTTP 200 (6.3MB), ffprobe h264
768×1344 6.58s = real 9:16 HD, NO download paywall (unlike ImagineArt), no watermark on
the eyeballed frame. See [[ai-video-gen-lab]].

DPR-SCALING gotcha: <html data-dpr-scaling="true"> makes element.click() land on <html>
and getBoundingClientRect y's compute off-screen — so EVERY interaction is a real
coordinate mouse click (pixel coords valid at the fixed 1280×900 viewport). Same lesson
as TensorArt's reactive-framework swallow.

PASSIVE capture: Leonardo's api is header-gated (direct calls 400), so we read the app's
own mp4 URLs off the wire (page.on response/request). Real output lives under
cdn.leonardo.ai/users/<uid>/generations/… — decoys (featured cards, model webms) are
skipped. Captured URLs are Cloudflare transcode wrappers
(…/cdn-cgi/media/mode=video,width=512/<ORIGINAL.mp4>); download() unwraps to the full-res
original.
"""

import os
import re
import time

from base import SiteAdapter, Wall

# promo / showcase / preset clips that are NOT our render
_DECOY = ("motion-control", "/presets/", "showcase", "tutorial", "onboarding",
          "/sample", "example", "featured-card", "leo-agent")
# unwrap Cloudflare's transcode prefix → the full-res original mp4
_CDN_CGI = re.compile(r'https?://[^ ]+?/cdn-cgi/media/[^/]+/(https?://.+\.mp4.*)$')


def _unwrap(url: str) -> str:
    m = _CDN_CGI.search(url or "")
    return m.group(1) if m else url


class Leonardo(SiteAdapter):
    name = "leonardo"
    home = "https://app.leonardo.ai/"
    credit_cost = 90                  # MiniMax H3; daily bucket is 150 ⇒ ~1 gen/day
    aspect = "9:16"

    # ---- passive capture of the app's own mp4 URLs (api is header-gated) ----
    def _watch(self, session):
        if getattr(self, "_watching", False):
            return
        self._mp4 = []

        def note(u):
            u = u or ""
            if ".mp4" in u and not any(d in u.lower() for d in _DECOY):
                self._mp4.append(u.split("#")[0])

        session.page.on("response", lambda r: note(r.url))
        session.page.on("request", lambda q: note(q.url))
        self._watching = True

    # ---- small perception helpers (all read visible button text) ----
    @staticmethod
    def _model_card(pg):
        return pg.evaluate("""() => {const b=[...document.querySelectorAll('button')]
          .find(e=>/^model\\b/i.test((e.innerText||'').trim()));
          return b?b.innerText.replace(/\\s+/g,' ').trim():null;}""")

    @staticmethod
    def _gen_cost(pg):
        return pg.evaluate("""() => {const b=[...document.querySelectorAll('button')]
          .find(e=>/^generate\\b/i.test((e.innerText||'').trim()));
          return b?b.innerText.replace(/\\s+/g,' ').trim():null;}""")

    @staticmethod
    def _res_labels(pg):
        return pg.evaluate("""() => [...document.querySelectorAll('button')]
          .map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim())
          .filter(t=>/\\d{3,4}\\s*[x×]\\s*\\d{3,4}/.test(t)).slice(0,4);""")

    @staticmethod
    def _generate_btn(pg):
        return pg.evaluate("""() => {const b=[...document.querySelectorAll('button')]
          .find(e=>/^generate\\b/i.test((e.innerText||'').trim()));
          if(!b)return null;const r=b.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")

    @staticmethod
    def _portrait(labels):
        for t in labels:
            m = re.search(r'(\d{3,4})\s*[x×]\s*(\d{3,4})', t)
            if m and int(m.group(2)) > int(m.group(1)):
                return True
        return False

    # ---- select MiniMax H3 in video mode ----
    def _pick_model(self, session):
        pg = session.page
        pg.mouse.click(601, 393); time.sleep(7)                 # enter Video mode
        pg.mouse.click(157, 121); time.sleep(2.5)               # open Model dropdown
        pg.mouse.click(591, 217); time.sleep(0.6)               # search box
        pg.keyboard.type("MiniMax", delay=40); time.sleep(2)
        pg.mouse.click(591, 320); time.sleep(2.5)               # first result
        for _ in range(2):
            pg.keyboard.press("Escape"); time.sleep(0.4)
        card = self._model_card(pg)
        if not card or "MiniMax" not in card:
            raise Wall("selector", f"MiniMax H3 not selected (model card='{card}')")

    # ---- type the prompt into the VISIBLE composer textarea ----
    #      (#home-prompt-textarea is a 0×0 hidden decoy; the real one is by geometry) ----
    def _type_prompt(self, session, prompt):
        pg = session.page
        pg.mouse.move(700, 400); pg.mouse.wheel(0, -12000); time.sleep(1.2)  # scroll to top
        tc = pg.evaluate("""() => {const ts=[...document.querySelectorAll('textarea')]
          .map(e=>({e,r:e.getBoundingClientRect()}))
          .filter(o=>o.r.width>150&&o.r.height>25&&o.r.y>50&&o.r.y<200).sort((a,b)=>a.r.y-b.r.y);
          if(!ts.length)return null;const o=ts[0];o.e.focus();o.e.setAttribute('data-me','1');
          return{x:Math.round(o.r.x+o.r.width/2),y:Math.round(o.r.y+o.r.height/2)};}""")
        if not tc:
            raise Wall("selector", "no visible composer prompt textarea")
        pg.mouse.click(tc["x"], tc["y"]); time.sleep(0.3)
        pg.keyboard.type(prompt, delay=16); time.sleep(0.8)
        typed = pg.eval_on_selector("textarea[data-me='1']", "e=>e.value") or ""
        if len(typed) < len(prompt) - 2:
            raise Wall("selector", f"prompt did not fully land ({len(typed)}/{len(prompt)}) — not spending")

    # ---- submit: pick model, set 9:16, GATE on portrait+cost, type prompt, Generate ----
    def submit(self, session, prompt: str):
        self._watch(session)
        self._prompt = prompt
        session.goto(self.home, wait="domcontentloaded"); time.sleep(8)
        pg = session.page
        for _ in range(2):
            pg.keyboard.press("Escape"); time.sleep(0.4)

        self._pick_model(session)
        pg.mouse.click(189, 412); time.sleep(0.8)               # 9:16

        # credit-protection gate: never spend unless the picker reads PORTRAIT and cost fits.
        res = self._res_labels(pg)
        cost = self._gen_cost(pg)
        ncost = int(re.search(r'(\d+)', cost or "0").group(1)) if cost and re.search(r'\d', cost) else 10 ** 9
        if not self._portrait(res):
            raise Wall("selector", f"aspect not portrait (labels={res}) — refusing to spend on wrong aspect")
        if ncost > 150:
            raise Wall("rate_limit", f"cost {ncost} > 150 daily tokens — refreshes daily")

        self._type_prompt(session, prompt)

        self._baseline = set(self._mp4)                         # existing clips, to spot the new one
        btn = self._generate_btn(pg)
        if not btn or not (0 < btn["y"] < 900):
            raise Wall("selector", f"Generate button not in viewport ({btn})")
        pg.mouse.click(btn["x"], btn["y"]); time.sleep(5)

    # ---- poll: watch the wire for a NEW mp4 (baseline-diff), else scan <video> elements ----
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
        return "rendering"

    # ---- download: unwrap the cdn-cgi transcode wrapper → full-res original ----
    def download(self, session, dest: str) -> str:
        url = getattr(self, "_done_url", None)
        if not url:
            raise Wall("selector", "no finished Leonardo render url to download")
        orig = _unwrap(url)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = session.page.request.get(orig)
        if resp.status != 200:
            raise Wall("block", f"download HTTP {resp.status}")
        with open(dest, "wb") as f:
            f.write(resp.body())
        return dest


ADAPTER = Leonardo()


if __name__ == "__main__":
    # self-check: the cdn-cgi transcode wrapper must unwrap to the full-res original, a bare
    # url must pass through untouched, and the portrait gate must read height>width only.
    wrapped = ("https://cdn.leonardo.ai/cdn-cgi/media/mode=video,width=512/"
               "https://cdn.leonardo.ai/users/u1/generations/hail_9x16.mp4?token=x")
    assert _unwrap(wrapped) == "https://cdn.leonardo.ai/users/u1/generations/hail_9x16.mp4?token=x"
    bare = "https://cdn.leonardo.ai/users/u1/generations/hail_9x16.mp4"
    assert _unwrap(bare) == bare, "bare original url must pass through"
    assert Leonardo._portrait(["768 x 1344"]) and not Leonardo._portrait(["1344 x 768", "1:1"])
    print("OK: cdn-cgi unwrap → original, bare passes through, portrait gate = height>width")
