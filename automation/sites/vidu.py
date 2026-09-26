"""
vidu.py — Vidu (vidu.com). FINAL-SHOT RECON 2026-09-23: AUTHENTICATED but economically
DEAD for free recurring use. The vidu.studio->vidu.com migration DID carry the session
(the old "logged-out on new domain" belief is WRONG — /iam/v1/users/me returns the real
account, subs_plan "free"); /create & /home redirect to /home/recommend (the real app).
The "Try Vidu" button is on the public landing only, not a logged-out signal.

But there is NO recurring free allowance: 4 STRANDED credits (credits_free:4), daily-login
bonus = 0 (/iam/v1/users/every-day-credit → credit:0), watch-ad mission awards 0, no sub.
A minimum text2video clip costs ~48 credits (server: /vidu/v1/tasks/credits →
cost_credits:48, can_submit:FALSE vs current 4). 9:16 IS settable (aspect popover:
16:9/9:16/1:1/4:3/3:4; prompt is a plain <textarea>, NOT ProseMirror). => fails the
recurring-free bar. INERT unless the user funds credits or a free allowance returns.

Health-check endpoints (host service.vidu.com): /credit/v1/credits/me (balance),
/iam/v1/users/me (auth), /vidu/v1/tasks/credits?type=text2video&settings.* (cost+can_submit),
/vidu/v1/tasks/history/me (results/download). See [[ai-video-gen-lab]].
"""
from base import SiteAdapter, Wall


class Vidu(SiteAdapter):
    name = "vidu"
    home = "https://www.vidu.com/"
    credit_cost = 48                  # min text2video clip (server: cost_credits 48)

    def submit(self, session, prompt: str):
        raise Wall("rate_limit", "vidu authed but 0 recurring free credits (4 stranded, need ~48) — not viable free")

    def poll(self, session) -> str:
        return "rate_limited"

    def download(self, session, dest: str) -> str:
        raise Wall("rate_limit", "vidu not wired — no free recurring credits")


ADAPTER = Vidu()
