"""
symphony.py — TikTok Symphony Creative Studio (ads.tiktok.com/creative/creativestudio).
Free tool running Dreamina Seedance 2.0; made-for-TikTok 9:16, weekly credits.

**OUT — GEO-BLOCKED (2026-09-23).** TikTok / the creative studio does not open in the
user's country (India), so login is impossible and the tool is unreachable. Kept as an
honest Wall rather than deleted, in case region access changes. Nothing to map/wire.
"""
from base import SiteAdapter, Wall


class Symphony(SiteAdapter):
    name = "symphony"
    home = "https://ads.tiktok.com/creative/creativestudio"
    credit_cost = 1
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("block", "symphony geo-blocked in region (India) — TikTok unreachable")

    def poll(self, session) -> str:
        return "error"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "symphony geo-blocked in region (India) — TikTok unreachable")


ADAPTER = Symphony()
