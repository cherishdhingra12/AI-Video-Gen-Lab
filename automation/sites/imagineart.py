"""
imagineart.py — ImagineArt (imagine.art). VALIDATED 2026-09-23: recurring-free + 9:16 +
video all check out, but the VIDEO OUTPUT IS PAYWALLED — disqualified.

Proven live on this account (org 560f251e…, plan_id 0):
  - 100 free DAILY credits (GET imagine.vyro.ai/v1/credit → total; verbatim toast "100
    free daily credits added"). Recurring-free: YES.
  - Composer https://www.imagine.art/video/create/wan-2-2 : model Wan 2.2, prompt =
    textarea[placeholder*="Describe the video"], aspect chip → 9:16 popover (settable,
    CONFIRMED), 5s/720p. Generate charged ~12cr and rendered a real 720×1280 (9:16) clip.
  - BUT free tier GATES the result: the History player overlays "Unlock to watch the full
    video / Upgrade to view", and the asset Download button hands back only the still
    KEYFRAME (ffprobe: mjpeg 720×1280, no duration) — never the mp4. No downloadable video
    URL is ever exposed to a free user (only promo/preset decoys on the wire).

=> generate-but-pay-to-view. Fails our "download a free 9:16 VIDEO" bar. INERT unless the
account is upgraded. (Headless UA-CH still leaks HeadlessChrome via userAgentData — not a
blocker here but note for any future wiring.) See [[ai-video-gen-lab]].
"""
from base import SiteAdapter, Wall


class ImagineArt(SiteAdapter):
    name = "imagineart"
    home = "https://www.imagine.art/video/create/wan-2-2"
    credit_cost = 30
    aspect = "9:16"

    def submit(self, session, prompt: str):
        raise Wall("block", "imagineart free tier paywalls the video output (Upgrade to view; free download = still keyframe only)")

    def poll(self, session) -> str:
        return "error"

    def download(self, session, dest: str) -> str:
        raise Wall("block", "imagineart video is pay-to-view — no free mp4")


ADAPTER = ImagineArt()
