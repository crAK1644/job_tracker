"""Playwright-backed job sources - only pay the browser cost for these.

Imported lazily from run.py; the plain-HTTP daily `fetch` path never imports this.

kariyer.net was in the original plan too, but live testing (2026-09-14) got an
"Access to this page has been denied" bot-block after two plain page loads, and
its ToS prohibits automated collection - so it's link-only in the report
(see tracker.kariyer_links), same treatment as LinkedIn, instead of scraped here.
"""
from __future__ import annotations

import json
import re

import sources

# hiring.cafe geo-defaults its "Latest jobs" view off these cookies. Set them
# explicitly so the result is Istanbul/Turkey-targeted regardless of the
# machine's real network location (verified live 2026-09-14).
_GEO_COOKIES = [
    {"name": "geo_country", "value": "TR", "domain": "hiringcafe.com", "path": "/"},
    {"name": "geo_city", "value": "Istanbul", "domain": "hiringcafe.com", "path": "/"},
    {"name": "geo_lat", "value": "41.01384", "domain": "hiringcafe.com", "path": "/"},
    {"name": "geo_lng", "value": "28.94966", "domain": "hiringcafe.com", "path": "/"},
]

_WORKPLACE_MAP = {"onsite": "onsite", "remote": "remote", "hybrid": "hybrid"}


def _map_workplace(raw: str) -> str:
    return _WORKPLACE_MAP.get((raw or "").strip().lower(), "unknown")


def _extract_ssr_hits(html: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("hiring.cafe: __NEXT_DATA__ not found (page structure changed)")
    data = json.loads(m.group(1))
    try:
        return data["props"]["pageProps"]["ssrHits"]
    except (KeyError, TypeError):
        return []


def fetch_hiringcafe() -> list[dict]:
    """hiring.cafe SSRs ~100 job hits straight into __NEXT_DATA__ - no need for
    the response-interception the plan assumed (verified live 2026-09-14).

    # ponytail: this is one un-paginated SSR batch (~100 hits), already
    # geo-filtered to Turkey by the cookies above. "Load more" triggers no
    # observable new XHR under passive scrolling, so there's no cheap way to
    # get more; revisit if 100/run turns out to be too few.
    """
    from playwright.sync_api import sync_playwright  # lazy import, browser dep

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(user_agent=sources.UA)
        context.add_cookies(_GEO_COOKIES)
        page = context.new_page()
        try:
            # networkidle never fires here (persistent analytics/posthog polling) -
            # the SSR data is in the initial document, so domcontentloaded is enough.
            page.goto("https://hiringcafe.com", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector("#__NEXT_DATA__", state="attached", timeout=15000)
            html = page.content()
        finally:
            browser.close()

    hits = _extract_ssr_hits(html)
    out = []
    for h in hits:
        info = h.get("job_information") or {}
        v5 = h.get("v5_processed_job_data") or {}
        company = (h.get("enriched_company_data") or {}).get("name") \
            or (h.get("attributed_org") or {}).get("name") or ""
        title = info.get("title") or ""
        tools = " ".join(v5.get("technical_tools") or [])
        activities = " ".join(v5.get("role_activities") or [])
        description = " ".join(filter(None, [
            v5.get("core_job_title"), v5.get("job_category"),
            v5.get("requirements_summary"), tools, activities,
        ]))
        out.append(sources.job(
            company, title, h.get("apply_url") or "",
            location=v5.get("formatted_workplace_location") or "",
            posted_at=v5.get("estimated_publish_date") or "",
            source="hiringcafe",
            description=description,
            raw_seniority=v5.get("seniority_level") or "",
            workplace=_map_workplace(v5.get("workplace_type")),
        ))
    return out


BROWSER_FETCHERS = {
    "hiringcafe": fetch_hiringcafe,
}
