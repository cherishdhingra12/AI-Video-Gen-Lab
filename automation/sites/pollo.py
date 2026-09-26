"""
pollo.py — Pollo.ai. Frontier aggregator (Seedance 2.5, Wan 3.0, MiniMax H3, Veo/Kling/
Sora behind one login). Free tier claimed monthly, but confirmed live 2026-09-23
(profile pollo__a1, "cherish dhingra's Project") the balance is DEPLETED: **5 credits**
left, and the default gens cost ~10-30 — so nothing can fire right now. The create tool
URL also 404'd from pollo.ai/create/text-to-video (real path still unmapped).

STUB — inert until (a) the free balance actually refreshes to a usable level AND (b) the
live text2video flow (prompt/aspect/generate/download) is mapped on a logged-in run. Do
not seed credits or wire selectors until the refresh cadence is confirmed on the account.
"""
from base import SiteAdapter, Wall


class Pollo(SiteAdapter):
    name = "pollo"
    home = "https://pollo.ai"
    credit_cost = 10

    def submit(self, session, prompt: str):
        raise Wall("rate_limit", "pollo balance depleted (5 credits) — awaiting refresh + flow mapping")

    def poll(self, session) -> str:
        return "rate_limited"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "pollo not wired (balance depleted)")


ADAPTER = Pollo()
