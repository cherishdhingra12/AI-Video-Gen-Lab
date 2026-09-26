"""
pika.py — Pika (pika.art). RECON 2026-09-23: AUTHENTICATED but NOT recurring-free.
Corrects the old "80 credits/MONTH recurring" belief — UNSUPPORTED. The real app is on a
SEPARATE host: create.pika.art (pika.art/ is marketing; "Open Pika" → create.pika.art/home;
/create and /my-library 404). Composer = create.pika.art/apps/create-video.

Auth PROVEN: creation-api.pika.art/v1/account returns the real account. 9:16 IS settable
(aspect popover: Auto/16:9/9:16/1:1/21:9/4:3/3:4/3:2/2:3, one click). BUT the free plan is
economically dead: /v1/credits → {credits:0}; /v1/billing/subscription → plan "free",
credits_reset_at:null (NO monthly refill). Generate shows "~⚡243" (Seedance 2.5/720p/10s);
cheapest listed t2v ~134cr. Free = pay-as-you-go top-up only ($25 min). => fails the
recurring-free bar. INERT unless the user tops up. See [[ai-video-gen-lab]].
"""
from base import SiteAdapter, Wall


class Pika(SiteAdapter):
    name = "pika"
    home = "https://create.pika.art/apps/create-video"
    credit_cost = 134                 # cheapest listed t2v clip; free balance is 0 anyway
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("rate_limit", "pika authed but free plan = 0 credits, no monthly reset — not viable free")

    def poll(self, session) -> str:
        return "rate_limited"

    def download(self, session, dest: str) -> str:
        raise Wall("rate_limit", "pika not wired — no free recurring credits")


ADAPTER = Pika()
