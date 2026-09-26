"""
domoai.py — Domo AI (domoai.app). Text-to-Video app lives at
domoai.app/create/text-to-video (the marketing /create is a 404). Seedance 2.0 /
DomoAI 2.4 models, prompt textarea + 'Ratio and duration' control (has 9:16) +
'Generate' button (~15 credits/gen at 480P).

OUT OF SCOPE — confirmed live 2026-09-23 (profile domoai__a1, logged in as
Domo_7135553): the free tier is **30 ONE-TIME credits, NOT monthly** (pricing panel:
Free = "30 one-time" vs Pro = "8,000+/mo"). A pure one-time grant is disqualified by
policy, so this adapter stays inert. Reinstate only if Domo adds a free daily/monthly
refresh. Selectors captured above so wiring is a 20-min job if that happens.
"""
from base import SiteAdapter, Wall


class DomoAI(SiteAdapter):
    name = "domoai"
    home = "https://www.domoai.app/create/text-to-video"
    credit_cost = 15
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("block", "domoai free tier is 30 ONE-TIME credits (not monthly) — out of scope")

    def poll(self, session) -> str:
        return "error"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "domoai out of scope (one-time credits)")


ADAPTER = DomoAI()
