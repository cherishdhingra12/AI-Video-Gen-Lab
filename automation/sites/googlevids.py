"""
googlevids.py — Google Vids (vids.google.com), Veo 3.1 clips. Documented as ~50 AI-clip
gens/month resetting 12 AM PT on the 1st (policy 2026-09-23: a monthly bucket is accepted).
The QUALITY pick of the fleet. 9:16 vertical supported since Oct 2025. That ~50/month is a
DOC, not a measurement: the account hit the limit for the first time on 2026-10-16 after
~50 generations this month, and the message it shows names no period — so treat the reset as
unknown and let the ledger re-ask (quota_refill) rather than assume the 1st.

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

Quota gate: when the pool is spent the panel says so — "You've hit your limits for generating
videos in Vids." with Try again / Remove — and that is a Wall("credits"), not a rate limit: it
means the ACCOUNT is out until its refill, so the pair is marked dry in the ledger rather than
cooled for a few minutes. The message names no period; see quota_refill.

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
    needs_cookie_refresh = False             # WAS True — since 2026-10-27 every Chrome import
    cookie_keywords = ["google"]             # yields a session the site rejects, and the refresh
                                             # CLOBBERS the profile's working login with it (it
                                             # destroyed all three Flow profiles that night; the
                                             # Vids ones survive only because the reel sat on
                                             # Flow). These profiles still hold valid sign-ins —
                                             # protect them: re-login via login.py when a profile
                                             # login-walls, never via the Chrome import.

    # text-based selectors so they survive class-name churn; CONFIRMED live 2026-09-23.
    prompt_sel = "textarea, [contenteditable='true']"     # AI-clip panel prompt box (1 match)
    generate_sel = "[aria-label='Generate' i]"            # send arrow; disabled until prompt non-empty

    # OUT OF GENERATIONS, in the words the panel actually uses (seen live 2026-10-16): "You've hit
    # your limits for generating videos in Vids." The second half of this pattern read
    # `generation`, and "generating" is not "generation" — so the one sentence that means STOP
    # matched NOTHING. Three submits read it as a render in progress, polled twelve minutes each,
    # recorded it as a transient `block` (10 min, doubling), spent one of the clip's three retries
    # and one generation apiece, and would have declared the clip dead. `generat` covers
    # generate/generating/generation. Checked against that live sentence, and against all five of
    # this reel's own prompts (they are typed into the panel, so they become page text) plus the
    # panel's own labels, for false hits.
    _QUOTA = re.compile(r"(reached|out of|no more|limit).{0,40}"
                        r"(credit|generat|clip|quota|limit)", re.I)

    # What that message means for the ledger. The words name a period only when they say so:
    # most refusals say "Try again" and nothing more, so the default stays 'daily' — a RE-ASK
    # interval, not a claim about Google's reset. Guessing 'monthly' would park a reel that
    # already owns clips on this source until the 1st if the cap were really daily, while
    # re-asking costs one refused browser run: no generation spent, and (runner) no retry.
    quota_refill = "daily"

    def refill_period(self, line: str) -> str:
        """The refill period the SITE'S OWN refusal words name — not a guess.

        Twice on 2026-10-24 a bucket refused in words that DO name the period: "You've reached
        your **monthly** limit. Wait until Nov 1 at 12:30 PM for limits to reset." The ledger
        still wrote Oct 25 00:00 (this file's daily default), so the next resume woke a day
        early, paid a refused browser run and re-parked with the same wrong date. The words are
        the one thing the site directly says about its reset — read them before inventing one.
        When the line names no period, the daily default stands, for exactly the reason above."""
        return "monthly" if re.search(r"\bmonthly\b", line or "", re.I) else self.quota_refill

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

    def _quota_line(self, session) -> str:
        """The site's own out-of-generations sentence, or '' when the page says nothing of the kind.

        The words matter twice over: they are why the wall fires, and they are what the job's
        `last_error` and the report show a person — "credits: You've hit your limits for generating
        videos in Vids." says exactly what happened, where a generic message would not."""
        try:
            body = session.page.inner_text("body")
        except Exception:
            return ""
        return next((l.strip() for l in body.splitlines() if self._QUOTA.search(l)), "")

    def _quota_blocked(self, session) -> bool:
        return bool(self._quota_line(session))

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
            raise Wall("credits", self._quota_line(session) or "Google Vids is out of generations",
                       refill=self.refill_period(self._quota_line(session)))

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
            raise Wall("credits", self._quota_line(session) or "Google Vids is out of generations",
                       refill=self.refill_period(self._quota_line(session)))

    def poll(self, session) -> str:
        """CONFIRMED live: the AI-clip panel shows a NN% + Cancel while Veo renders; when
        done the result action bar (Insert / Extend / Edit / Recreate / Remove) appears.
        ~60s for a 10s clip. Out of generations -> Wall('credits'), so the runner marks the
        pair dry with its refill time instead of cooling it for a while."""
        pg = session.page
        try:
            body = pg.inner_text("body")
        except Exception:
            body = ""
        if self._QUOTA.search(body):
            # OUT, not throttled. Returning "rate_limited" here sent this to
            # learn._WALL_BASE['rate_limit']: a 15-minute cooldown that spends one of the clip's
            # three retries and never marks the pair dry — so the same empty account was picked
            # again, and again, until the clip was declared dead and a crisis was filed over what
            # is really just a refill. Being empty is not a mistake to step back from.
            line = next((l.strip() for l in body.splitlines() if self._QUOTA.search(l)), "")
            raise Wall("credits", line or "Google Vids is out of generations",
                       refill=self.refill_period(line))
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


    # ---- live balance: Vids does not have one to read ----
    def read_credits(self, session):
        """ALWAYS None: Vids shows no balance anywhere, so the generic vision reader must not be
        pointed at it. Confirmed by eye (2026-10-16) at all three levels of the UI — the home page,
        the editor, and the AI-video panel WITH a prompt typed, where the row reads only
        'Omni · 720p · 3s' and a Generate arrow. Given nothing to read, the vision reader invents
        a number: on the good-morning reel it answered 0, `observe_credits` stored that as an
        OBSERVED fact, and the site went dry until the 1st with ~48 generations left — the same
        dead-end a hand-entered 0 causes, now written by the machine.

        None means "unknown", which is the truth here: it keeps the last known count and lets
        `spend_credit` walk it down accurately (1 clip = 1 credit). When the pool really is
        spent, Vids says so in words — `submit()` raises Wall('credits') and mark_dry() records
        the month's reset. The site is asked, and it answers; nobody guesses on its behalf."""
        return None


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

    # The vision reader must never be given this page: it answered 0 on a page with no balance
    # and that 0 marked a funded site dry for a month. Unknown is the correct answer here.
    assert a.read_credits(s2) is None, "Vids has no readable balance — never invent one"
    print("googlevids reports an unknown balance, never a fabricated one")

    # OUT OF GENERATIONS, in the site's own words. This is the sentence the pipeline READ AS A
    # RENDER IN PROGRESS for three submits and 36 minutes of polling (2026-10-16): the pattern
    # wanted "generation" and the panel says "generating". Both halves matter — a wall that never
    # fires strands a reel, and one that fires on anything swallows good renders — so the live
    # sentence is asserted IN, and the page text a healthy panel shows is asserted OUT.
    class _P:
        def __init__(self, body): self._b = body
        def inner_text(self, *_a): return self._b
    class _S:
        def __init__(self, body): self.page = _P(body)
    LIVE = "You've hit your limits for generating videos in Vids.\nTry again\nRemove"
    assert a._quota_blocked(_S(LIVE)), "the site's own 'you are out' sentence must raise a wall"
    assert "limits for generating videos" in a._quota_line(_S(LIVE)), \
        "the wall must carry the site's words — they are what the report shows a person"
    import learn
    assert learn.refill_at(a.refill_period("")) > time.time(), \
        f"the ledger needs a real time from an unnamed refusal: {a.quota_refill!r}"
    assert a.refill_period("You've reached your monthly limit. Wait until Nov 1 at 12:30 PM "
                           "for limits to reset.") == "monthly", \
        "a refusal that names the period IN WORDS must be believed — the daily guess woke " \
        "the ledger a day early twice on 2026-10-24"
    assert a.refill_period("Try again tomorrow.") == "daily", \
        "a refusal that names no period keeps the daily default — do not exaggerate a guess"
    HEALTHY = ("AI video clip\nOmni · 720p · 3s\nGenerate an AI video clip\nInsert\nRecreate\n"
               "REQUIRED OUTPUT: 3-D ANIMATED VIDEO — a stylized animated-feature look in every "
               "frame\nNo text of any kind anywhere in the frame EXCEPT on a phone or tablet "
               "screen\nAll caption cards are added later.")
    assert not a._quota_blocked(_S(HEALTHY)), \
        "a render in progress must never be read as an empty account — the reel's own prompt is " \
        "typed into this page, so it is page text too"
    print("googlevids reads the site's real out-of-generations sentence as a wall, and nothing else")

