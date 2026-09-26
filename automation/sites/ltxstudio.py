"""
ltxstudio.py — LTX Studio (ltx.io/studio). Frontier LTXV-13B; free daily-credit tier.
STUB: home only, for cookie import + daily-panel check. Storyboard-oriented UI — the
plain text->video path needs first-run confirmation.
"""
from base import SiteAdapter, Wall


class LTXStudio(SiteAdapter):
    name = "ltxstudio"
    home = "https://ltx.io/studio"
    credit_cost = 1

    def submit(self, session, prompt: str):
        raise Wall("selector", "ltxstudio submit not wired yet")

    def poll(self, session) -> str:
        return "rendering"

    def download(self, session, dest: str) -> str:
        raise Wall("selector", "ltxstudio download not wired yet")


ADAPTER = LTXStudio()
