"""
dreamina.py — Dreamina (dreamina.capcut.com, ByteDance/CapCut, runs Seedance 2.5). Free
tier is daily-refreshing AND the quality is frontier, so on paper this was the best "other"
pick. BUT confirmed live 2026-09-23 (profile dreamina__a1): the video generator is
account/region GATED — /ai-tool/generate?type=video shows a "Coming up soon..." Seedance
preview splash and any entry redirects to /ai-tool/no-use-permission?type=video. The
account has no permission to run video generation, so nothing can be wired.

STUB — inert until Dreamina grants this account video access (region change / rollout).
If it opens up: TipTap/textarea prompt + Generate, results in a right-hand history rail,
9:16 aspect control — map on the first permitted run.
"""
from base import SiteAdapter, Wall


class Dreamina(SiteAdapter):
    name = "dreamina"
    home = "https://dreamina.capcut.com/ai-tool/generate/?type=video"
    credit_cost = 4
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("block", "dreamina video gated: account has no-use-permission (region/rollout)")

    def poll(self, session) -> str:
        return "error"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "dreamina video not permitted on this account")


ADAPTER = Dreamina()
