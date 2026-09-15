"""Playwright-based ATS auto-discovery for companies.yaml entries with ats: unknown.

Crawls a company's own site (careers page + likely subdomains), watches both the
rendered DOM and the outgoing network requests for a known ATS signature, then
verifies the extracted token by actually fetching that board through
`sources.fetch_company` before trusting it. Because the token comes from a link
found *on the company's own domain* (not guessed), this sidesteps the
greenhouse/insider -> Business Insider trap that blind token-guessing runs into.

Imported lazily by run.py - the plain-HTTP daily `fetch` path never pays the
browser cost.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import sources

CANDIDATE_PATHS = [
    "", "/careers", "/kariyer", "/careers/jobs", "/en/careers", "/tr/kariyer",
    "/jobs", "/careers/", "/company/careers",
]
CANDIDATE_SUBDOMAINS = ["careers.{d}", "kariyer.{d}", "jobs.{d}"]

# (regex over a URL, ats name). First capture group is the token.
# Families with no `sources.fetch_<ats>` (yet) are still recognized so a hit
# gets reported instead of silently looking like "no ATS at all" - see
# UNSUPPORTED_ATS below.
SIGNATURES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:boards|job-boards)\.(?:eu\.)?greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
    (re.compile(r"apply\.workable\.com/(?:api/v\d/accounts/)?([a-z0-9_-]+)", re.I), "workable"),
    (re.compile(r"(?:careers|api)\.smartrecruiters\.com/(?:v1/companies/)?([a-z0-9_-]+)", re.I), "smartrecruiters"),
    (re.compile(r"([a-z0-9_-]+)\.recruitee\.com", re.I), "recruitee"),
    (re.compile(r"([a-z0-9_-]+)\.bamboohr\.com", re.I), "bamboohr"),
    (re.compile(r"([a-z0-9_-]+)\.jobs\.personio\.(?:de|com)", re.I), "personio"),
    (re.compile(r"([a-z0-9_-]+)\.teamtailor\.com", re.I), "teamtailor"),
    # Recognized but no fetcher written yet - common enough in the TR market
    # to be worth flagging for manual follow-up rather than reported as "none".
    (re.compile(r"careers-page\.com/([a-z0-9_-]+)", re.I), "careers-page"),
    (re.compile(r"successfactors\.(?:eu|com)/([a-z0-9_-]+)", re.I), "successfactors"),
    (re.compile(r"([a-z0-9_-]+)\.jobvite\.com", re.I), "jobvite"),
]
WORKDAY_RX = re.compile(r"([a-z0-9_-]+)\.(wd\d)\.myworkdayjobs\.com/(?:wday/cxs/[a-z0-9_-]+/)?([a-z0-9_-]+)", re.I)

# ATS families we can recognize on sight but don't have a sources.fetch_<ats>
# for - report the hit instead of pretending nothing was found, so a human can
# add support or fill the token in by hand.
# These portals use the HTML fallback in sources.py. Keep the set for future
# signatures that are detected but still have no public collector.
UNSUPPORTED_ATS: set[str] = set()

# Hosts that are ATS-shaped but not actually the company's own board (widgets,
# CDNs, generic pixel trackers that happen to match a loose regex).
IGNORE_SUBSTR = ("googletagmanager", "google-analytics", "doubleclick")


def _scan_signature(url: str) -> tuple[str, str] | None:
    if any(s in url for s in IGNORE_SUBSTR):
        return None
    for rx, ats in SIGNATURES:
        m = rx.search(url)
        if m:
            return ats, m.group(1).lower()
    m = WORKDAY_RX.search(url)
    if m:
        tenant, wd, site = m.groups()
        return "workday", f"{tenant}|{wd}|{site}"
    return None


def probe_company(page, domain: str, timeout_ms: int = 15000) -> tuple[str, str] | None:
    """Load candidate URLs for `domain`, watch network + DOM, return (ats, token) or None."""
    found: dict[str, tuple[str, str]] = {}

    def on_request(req):
        hit = _scan_signature(req.url)
        if hit:
            found.setdefault(hit[0], hit)

    page.on("request", on_request)
    try:
        candidates = [f"https://{domain}{p}" for p in CANDIDATE_PATHS]
        candidates += [f"https://{sd.format(d=domain)}" for sd in CANDIDATE_SUBDOMAINS]
        visited: set[str] = set()

        # A careers landing page often links to /join-us or /open-positions
        # before it embeds an ATS. Follow a small, same-domain queue instead
        # of assuming every employer uses the conventional /careers path.
        while candidates and len(visited) < 24:
            url = candidates.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                continue
            if found:
                break
            try:
                hrefs = page.eval_on_selector_all(
                    "a[href], iframe[src]",
                    "els => els.map(e => e.href || e.src).filter(Boolean)",
                )
            except Exception:
                hrefs = []
            for href in hrefs:
                hit = _scan_signature(href)
                if hit:
                    found.setdefault(hit[0], hit)
                    continue
                parsed = urlsplit(href)
                same_company = parsed.hostname and (
                    parsed.hostname == domain or parsed.hostname.endswith(f".{domain}"))
                if same_company and re.search(r"career|kariyer|job|position|ilan|join", parsed.path, re.I):
                    candidates.append(href)
            if found:
                break
    finally:
        page.remove_listener("request", on_request)

    if not found:
        return None
    return next(iter(found.values()))  # first ATS family hit, in candidate-priority order


def discover_unknown(companies: list[dict], client, only: set[str] | None = None,
                      progress=print) -> list[dict]:
    """Probe every `ats: unknown` entry in `companies` (or just `only` names).

    Returns one result dict per attempted company:
    {name, ats, token, verified, job_count}. `verified` is True only when the
    extracted token was fetched and returned at least one real posting.
    """
    from playwright.sync_api import sync_playwright  # lazy import, browser dep

    targets = [c for c in companies if c.get("ats") in (None, "unknown")]
    if only:
        targets = [c for c in targets if c["name"] in only]

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for entry in targets:
                # One page per company - a shared page leaked state across
                # probes (redirects/history from company N still loaded when
                # company N+1 started), which is what produced a Doist ->
                # ashby:toggl cross-attribution. Verified live 2026-09-14.
                page = browser.new_page(user_agent=sources.UA)
                try:
                    hit = probe_company(page, entry["domain"])
                finally:
                    page.close()
                if not hit:
                    progress(f"  ????  {entry['name']:<20} no ATS signature found")
                    results.append({"name": entry["name"], "ats": None, "token": None,
                                     "verified": False, "job_count": 0})
                    continue
                ats, token = hit
                if ats in UNSUPPORTED_ATS:
                    progress(f"  ????  {entry['name']:<20} found {ats}:{token} "
                             f"(no fetcher yet - add manually)")
                    results.append({"name": entry["name"], "ats": ats, "token": token,
                                     "verified": False, "job_count": 0})
                    continue
                try:
                    jobs = sources.fetch_company({**entry, "ats": ats, "token": token}, client)
                except Exception as e:  # noqa: BLE001
                    progress(f"  BAD   {entry['name']:<20} {ats}:{token} -> {type(e).__name__}: {e}")
                    results.append({"name": entry["name"], "ats": ats, "token": token,
                                     "verified": False, "job_count": 0})
                    continue
                verified = len(jobs) > 0
                tag = "OK   " if verified else "EMPTY"
                progress(f"  {tag} {entry['name']:<20} {ats}:{token:<20} {len(jobs)} jobs")
                results.append({"name": entry["name"], "ats": ats, "token": token,
                                 "verified": verified, "job_count": len(jobs)})
        finally:
            browser.close()
    return results
