"""
veo.py — Google Veo 3 via Flow (labs.google/flow). Best quality on the list,
stingiest free tier. First in the waterfall for quality-critical clips.

SELECTORS best-effort — confirm on first logged-in run (see kling.py note). Veo/Flow
is a Google SPA; the vision poll fallback covers selector drift.
"""

from base import SiteAdapter


class Veo(SiteAdapter):
    name = "veo"
    home = "https://labs.google/fx/tools/flow"
    credit_cost = 1                          # Flow counts in "credits"; verify at signup

    prompt_sel = "textarea, [contenteditable='true']"
    generate_sel = "button:has-text('Create'), button:has-text('Generate')"
    download_sel = "button[aria-label*='download' i], a[download], [class*='download' i]"


ADAPTER = Veo()
