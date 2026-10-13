"""
tensorart.py — TensorArt (tensor.art). Daily-refresh CONFIRMED (api user/credits →
dailyAmount, hasAccountTypes includes "DAILY"; the ⚡ balance in the UI is that same daily
bucket, ~50/day). Model Wan2.2-t2v, 9:16 available, ~18.32 credits/gen ⇒ ~2 vids/day.

TURNSTILE (resolved 2026-09-23): the Cloudflare "Verify you are human" challenge fires
only ONCE per profile. After a human solves it once in Chrome and import_login.py copies
the cf_clearance cookie into this profile, Generate no longer challenges (proven live:
t0 NO turnstile, no reappearance). submit() therefore only raises Wall("captcha") if a
Turnstile actually shows — it usually won't; when it does, the fix is one manual solve in
Chrome + `import_login.py tensorart <acct>`.

CREDITS: Generate is server-gated. Below cost the app toasts
"WORKS_INSUFFICIENT_CREDITS_NO_VIP|insufficient credits" and no task is created (dailyAmount
unchanged). submit() detects this and raises Wall("rate_limit") so the runner cools
TensorArt until the daily bucket refreshes, then waterfalls. ~2 free gens/day.

All api reads are PASSIVE captures of the app's own responses (api.tensor.art requires
x-request-sign* headers we can't forge, so direct calls return empty — we never replay).
Aspect + Generate use REAL mouse clicks; JS el.click()/value-injection is swallowed by the
reactive framework. FULLY PROVEN end-to-end 2026-09-23: our 9:16/480p/3s lantern render
(status FINISH in works/tasks/query) downloaded + ffprobe-confirmed **480x832 h264 3.06s**.
Finished works are R2 PRESIGNED URLs — _extract keeps the ?X-Amz-… query (stripping it 400s).
"""

import os
import re
import time
import json

from base import SiteAdapter, Wall

_API_CREDITS = "user/credits"
_API_TASKS = "works/tasks/query"      # poll source (POST, app-fired)
_API_ENTRY = "entry/list"             # finished works (download source)


class TensorArt(SiteAdapter):
    name = "tensorart"
    home = "https://tensor.art"
    credit_cost = 18                  # "Generate ⚡18.32", rounded
    aspect = "9:16"

    # ---- passive capture: api.tensor.art needs signed headers, so we never call it
    #      ourselves — we read the app's own responses off the wire. ----
    def _watch(self, session):
        if getattr(self, "_watching", False):
            return
        self._cap = {}
        def on_resp(r):
            u = r.url or ""
            for key in (_API_CREDITS, _API_TASKS, _API_ENTRY):
                if key in u:
                    try:
                        self._cap[key] = r.json()
                    except Exception:
                        pass
        session.page.on("response", on_resp)
        self._watching = True

    def _daily(self):
        return (self._cap.get(_API_CREDITS, {}) or {}).get("data", {}).get("dailyAmount")

    def _tasks(self):
        return (self._cap.get(_API_TASKS, {}) or {}).get("data", {}).get("tasks") or []

    def _entries(self):
        return (self._cap.get(_API_ENTRY, {}) or {}).get("data", {}).get("results") or []

    # ---- reach the Text2Video / Wan2.2-t2v composer ----
    def _open_composer(self, session):
        pg = session.page
        pg.mouse.click(855, 32); time.sleep(1.5)   # green Create caret
        pg.mouse.click(755, 32); time.sleep(4)      # studio
        pg.mouse.click(290, 65); time.sleep(4)      # Video tab
        hit = pg.evaluate("""() => {const el=[...document.querySelectorAll('*')]
          .filter(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
            return t==='Text2Video'&&r.width>0;})
          .sort((a,b)=>a.getBoundingClientRect().width-b.getBoundingClientRect().width)[0];
          if(!el)return null;const r=el.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not hit:
            raise Wall("selector", "Text2Video sub-tab not found")
        pg.mouse.click(hit["x"], hit["y"]); time.sleep(5)
        if not pg.evaluate("() => /wan2\\.2/i.test(document.body.innerText||'')"):
            raise Wall("selector", "Wan2.2-t2v model not present in composer")

    def _type_prompt(self, session, prompt):
        pg = session.page
        box = pg.evaluate("""() => {const b=[...document.querySelectorAll('textarea')]
          .find(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.x<400;});
          if(!b)return null;const r=b.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not box:
            raise Wall("selector", "prompt textarea not found")
        pg.mouse.click(box["x"], box["y"]); time.sleep(0.3)
        # clear any residual text, then REAL-type (JS value-injection is ignored by the
        # reactive framework — the button stays enabled but submits an empty prompt).
        for combo in ("Control+A", "Meta+A"):
            pg.keyboard.press(combo)
        pg.keyboard.press("Delete"); time.sleep(0.3)
        pg.keyboard.type(prompt, delay=22); time.sleep(1.2)

    @staticmethod
    def _locate(pg, exact):
        """Center of the smallest visible element whose text == exact (for real mouse clicks)."""
        return pg.evaluate("""(tx) => {const el=[...document.querySelectorAll('*')]
          .filter(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
            return t===tx&&r.width>10&&r.height>8;})
          .sort((a,b)=>a.getBoundingClientRect().width-b.getBoundingClientRect().width)[0];
          if(!el)return null;const r=el.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""", exact)

    @staticmethod
    def _pill(pg):
        """Bottom-left aspect pill (text + center). Reads e.g. '9:16/720p' or '16:9/720p'."""
        return pg.evaluate("""() => {const el=[...document.querySelectorAll('*')]
          .find(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
            return /^(9:16|16:9|1:1)\\/?\\d*p?$/.test(t)&&r.x<200&&r.y>740&&r.width>18&&r.width<160;});
          if(!el)return null;const r=el.getBoundingClientRect();
          return{t:el.innerText.replace(/\\s+/g,' ').trim(),x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")

    def _set_aspect(self, session):
        # The dropdown is a 2-column picker (ratio left, resolution right). JS el.click() is
        # ignored by the reactive framework — must use REAL mouse clicks on element centers.
        pg = session.page
        pill = self._pill(pg)
        if not pill:
            raise Wall("selector", "aspect pill not found")
        pg.mouse.click(pill["x"], pill["y"]); time.sleep(1.4)     # open dropdown
        opt = self._locate(pg, "9:16")                            # ratio column
        if opt:
            pg.mouse.click(opt["x"], opt["y"]); time.sleep(1.1)
        res = self._locate(pg, "9:16/720p")                       # resolution column
        if res:
            pg.mouse.click(res["x"], res["y"]); time.sleep(1.1)
        pg.keyboard.press("Escape"); time.sleep(0.5)
        now = self._pill(pg)
        if not (now and now["t"].startswith("9:16")):
            raise Wall("selector", f"could not confirm 9:16 (pill='{now and now['t']}') — refusing to spend on wrong aspect")

    @staticmethod
    def _turnstile(session):
        try:
            if re.search(r"verify you are human", session.page.inner_text("body"), re.I):
                return True
        except Exception:
            pass
        return any("challenges.cloudflare" in (f.url or "") for f in session.page.frames)

    @staticmethod
    def _insufficient(session):
        # server toast when the daily bucket can't cover the gen; no task is created
        try:
            return bool(re.search(r"insufficient[_ ]?credits", session.page.inner_text("body"), re.I))
        except Exception:
            return False

    @staticmethod
    def _ids(items):
        return {it.get("id") or it.get("taskId") for it in items}

    @staticmethod
    def _extract(o):
        """(prompt, status, mp4_url) from a task/entry object (we only get JSON, no fields promised)."""
        blob = json.dumps(o)
        pr = ""
        for k in ("prompt", "inputPrompt", "text"):
            m = re.search(r'"%s"\s*:\s*"([^"]{0,120})"' % k, blob)
            if m:
                pr = m.group(1); break
        st = str(o.get("status") or o.get("state") or "").upper()
        # Every task/entry object embeds the MODEL's showcase demo clip (…/model_showcase/…,
        # a 1080x1080 6s decoy) alongside — or before — our real output. A blind "first mp4"
        # grabs the decoy (proven: our PROCESSING lantern task served a 1080x1080/6.23s
        # showcase). Take the first mp4 that is NOT a model_showcase asset.
        # Finished works are R2 PRESIGNED URLs — the ?X-Amz-… signature query is MANDATORY
        # (stripping it → HTTP 400). Keep everything up to a quote/backslash/space.
        clean = blob.replace('\\u002F', '/').replace('\\/', '/')
        urls = re.findall(r'https?://[^"\\ ]+\.mp4(?:\?[^"\\ ]*)?', clean)
        out = next((u for u in urls if "model_showcase" not in u), None)
        return pr.lower(), st, out

    _INFLIGHT = {"PROCESSING", "PENDING", "QUEUED", "RUNNING", "CREATING", "WAITING", "0", "1"}

    def _mine(self, o):
        """True if this task/entry is OUR just-submitted gen (prompt match), not a stale one.
        Guards the empty-baseline trap: id-diff alone grabbed unrelated finished clips."""
        pr, _, _ = self._extract(o)
        head = (getattr(self, "_prompt", "") or "")[:30].lower()
        return bool(head) and head in pr

    # ---- submit ----
    def submit(self, session, prompt: str):
        self._watch(session)
        self._prompt = prompt                 # remembered so poll matches OUR render, not a stale clip
        session.goto(self.home); time.sleep(9)
        self._open_composer(session)
        self._type_prompt(session, prompt)
        self._set_aspect(session)

        btn = session.page.evaluate("""() => {const cs=[...document.querySelectorAll('button,[role=button],div,span')]
          .filter(e=>{const t=(e.innerText||'').replace(/\\s+/g,' ').trim();const r=e.getBoundingClientRect();
            return /^generate\\b/i.test(t)&&r.width>40&&r.height>15&&r.height<120;})
          .sort((a,b)=>{const A=a.getBoundingClientRect(),B=b.getBoundingClientRect();
            return (A.width*A.height)-(B.width*B.height);});
          const b=cs[0];if(!b)return null;const r=b.getBoundingClientRect();
          return{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
        if not btn:
            raise Wall("selector", "Generate button not found")
        session.page.mouse.click(btn["x"], btn["y"]); time.sleep(4)

        # Turnstile only fires on a profile that hasn't solved it once (see header). If it
        # shows, we can't clear it autonomously — Wall so the runner waterfalls; the fix is
        # one manual solve in Chrome + import_login.py.
        if self._turnstile(session):
            raise Wall("captcha", "TensorArt Turnstile on Generate — solve once in Chrome then import_login")
        # Server-gated on credits: below cost it toasts insufficient-credits and creates no
        # task. Treat as a cooldown until the daily bucket refreshes (~2 free gens/day).
        for _ in range(4):
            if self._insufficient(session):
                raise Wall("credits", f"insufficient daily credits (have {self._daily()}, need ~{self.credit_cost}) — refreshes daily",
                               refill="daily")
            time.sleep(2)

    # ---- poll: match OUR render by prompt (finished works land in entry/list; tasks/query
    #      shows in-flight status). Proven live: a PROCESSING task's mp4 is a preview, so we
    #      only accept a finished one — matched by prompt to dodge the empty-baseline mis-grab. ----
    def poll(self, session) -> str:
        try:
            session.page.reload(wait_until="commit"); time.sleep(6)   # re-fire tasks/query + entry/list
        except Exception:
            pass
        # finished works
        for it in self._entries():
            if self._mine(it):
                pr, st, mp = self._extract(it)
                if mp:
                    self._done_url = mp
                    return "done"
        # a task that has left the in-flight states and carries a final mp4
        for t in self._tasks():
            if self._mine(t):
                pr, st, mp = self._extract(t)
                if mp and st not in self._INFLIGHT:
                    self._done_url = mp
                    return "done"
        return "rendering"

    # ---- download ----
    def download(self, session, dest: str) -> str:
        url = getattr(self, "_done_url", None)
        if not url:
            raise Wall("selector", "no finished TensorArt render url to download")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = session.page.request.get(url.split("#")[0])   # CDN asset, not signed api
        if resp.status != 200:
            raise Wall("block", f"download HTTP {resp.status}")
        with open(dest, "wb") as f:
            f.write(resp.body())
        return dest


ADAPTER = TensorArt()


if __name__ == "__main__":
    # self-check: _extract must skip the model_showcase decoy and pick the real output,
    # and _mine must match our prompt while rejecting a stranger's.
    a = TensorArt(); a._prompt = "a red paper lantern rising through foggy bamboo at dusk"
    decoy = {"status": "PROCESSING", "prompt": a._prompt,
             "url": "https://image.tensorartassets.com/model_showcase/0/x.mp4"}
    both = {"status": "SUCCESS", "prompt": a._prompt,
            "showcase": "https://image.tensorartassets.com/model_showcase/0/x.mp4",
            "output": "https://cdn.tensor.art/out/real_9x16.mp4"}
    # a real R2 presigned URL: the ?X-Amz-… query MUST survive extraction or download 400s
    signed = {"status": "FINISH", "prompt": a._prompt,
              "url": "https://tensor-sf-sig.r2.cloudflarestorage.com/w/x.mp4?X-Amz-Signature=abc123&X-Amz-Expires=3600"}
    assert a._extract(decoy)[2] is None, "should not grab the showcase decoy"
    assert a._extract(both)[2] == "https://cdn.tensor.art/out/real_9x16.mp4", "should pick real output"
    assert a._extract(signed)[2].endswith("X-Amz-Expires=3600"), "must keep the presigned query"
    assert a._mine(both) and not a._mine({"prompt": "76 year old man lifting weights"})
    print("OK: _extract skips decoy, keeps presigned query, picks real output; _mine matches ours only")

