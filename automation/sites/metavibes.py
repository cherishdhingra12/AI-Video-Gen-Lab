"""
metavibes.py — Meta Vibes (meta.ai/vibes + Meta AI app). Free, Movie Gen model, up to 16s,
9:16-capable. HARD-BLOCKED 2026-09-23: the logged-in account is age-restricted — Vibes returns
"Teens under 18 can't use Vibes". That gate is independent of (and comes before) the other open
risk: a clean MP4 download-to-device was never confirmed (Vibes is built to share to feed/IG/FB/
DM). Not viable on this account; revisit only with an unrestricted account AND a proven file export.
"""
from base import SiteAdapter, Wall


class MetaVibes(SiteAdapter):
    name = "metavibes"
    home = "https://www.meta.ai/vibes"
    credit_cost = 1           # no published limit
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("block", "metavibes age-restricted on this account ('Teens under 18 can't use Vibes'); needs an unrestricted account + proven file export")

    def poll(self, session) -> str:
        return "error"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "metavibes age-restricted + download-to-file unconfirmed")


ADAPTER = MetaVibes()
