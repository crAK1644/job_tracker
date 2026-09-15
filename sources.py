"""Job sources.

Every fetcher returns a list of dicts with the same shape, so nothing downstream
needs to know where a job came from:

    {uid, company, title, url, location, workplace, posted_at, source,
     description, raw_seniority}

Group A: direct ATS APIs (open JSON, no auth, no ToS problem) - the backbone.
Group B: public job-board APIs / feeds.
Group C: Playwright-backed sources, lazily imported (see browser_sources.py).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(30.0, connect=15.0)
MAX_PAGES = 12  # ponytail: hard page cap on every paginator, no endless-loop guard needed

# himalayas caps `limit` at 20 whatever you ask for and publishes ~2900 jobs a
# day, so MAX_PAGES=12 saw the newest 240 - about four hours of a feed the
# tracker reads once a day. Depth is therefore a time window, not a page count:
# page until the feed timestamps fall past HIMALAYAS_DAYS. Two days measured
# live 2026-09-15 at 286 pages / 5720 jobs / 260s, closing on the window with no
# 429; three days needs ~430 pages and is not worth the daily cost. The page
# number is only a backstop against a feed whose ordering stops descending -
# left high enough that it never fires on a normal run, since firing it marks
# the source truncated and exempts it from the closing sweep.
HIMALAYAS_DAYS = 2
HIMALAYAS_MAX_PAGES = 500
# ponytail: flat sleep, not a token bucket - one fetcher, one thread. The feed
# rate-limits on a rolling budget, not a fixed rate: 0.4s ran clean for 336
# pages and then 429'd, 0.8s finished 286 pages untouched. Raise HIMALAYAS_DAYS
# and this likely needs raising too.
HIMALAYAS_PAGE_DELAY = 0.8

# Sources that stopped early while their own payload still advertised more.
# A truncated source keeps the jobs it did collect, but must be kept OUT of the
# closing sweep: "absent from this fetch" stops meaning "gone from the board",
# and a partial fetch is otherwise indistinguishable from a healthy one. Read
# and cleared per run by run.py, which is also why this is module state rather
# than a return value - it would otherwise have to be threaded through every
# fetcher signature and both dispatch dicts for a case that is nearly always
# empty.
TRUNCATED: dict[str, str] = {}


def note_truncated(tag: str, got: int, expected, cap: int = MAX_PAGES) -> None:
    TRUNCATED[tag] = f"fetched {got} of {expected}, stopped at page cap {cap}"



# --------------------------------------------------------------------- helpers

class _RetryTimeout(httpx.HTTPTransport):
    """Retry a read timeout once.

    httpx's own `retries=` covers connect errors only, and the failure this
    board set actually produces is a read timeout: youthall dropped its whole
    36-job board on 2026-09-14 because one of four pages was slow. Losing a
    source outright is a much worse outcome than one extra request.
    """

    MIN_HOST_INTERVAL = 0.15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_request: dict[str, float] = {}

    def handle_request(self, request):
        host = request.url.host or ""
        now = time.monotonic()
        wait = self.MIN_HOST_INTERVAL - (now - self._last_request.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_request[host] = time.monotonic()
        try:
            return super().handle_request(request)
        except httpx.TimeoutException:
            # One bounded retry is enough for the intermittent board stalls we
            # see in practice; the short backoff avoids immediately repeating
            # a congested request on the same host.
            time.sleep(0.5)
            self._last_request[host] = time.monotonic()
            return super().handle_request(request)


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"},
        timeout=TIMEOUT,
        follow_redirects=True,
        transport=_RetryTimeout(retries=2),
    )


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    if "<" not in raw:
        return " ".join(raw.split())
    return " ".join(HTMLParser(raw).text(separator=" ").split())


def canonical_url(url: str) -> str:
    """Remove tracking parameters while preserving vacancy-identifying ones."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    tracking = {"gclid", "fbclid", "mc_cid", "mc_eid", "lever-source", "source"}
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in tracking]
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def make_uid(company: str, title: str, url: str, external_id: str = "") -> str:
    # Keep the historical ordering for ordinary URLs. Existing job rows use
    # this hash as their stable primary key, so changing it would turn every
    # refresh into a new listing and lose the user's workflow history.
    if external_id:
        key = f"{(company or '').strip().lower()}|{external_id}|{_norm_title(title)}"
    else:
        key = f"{(company or '').strip().lower()}|{_norm_title(title)}|{canonical_url(url)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_REMOTE = re.compile(r"remote|uzaktan|work from home|anywhere|distributed", re.I)
_HYBRID = re.compile(r"hybrid|hibrit|karma", re.I)


def guess_workplace(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    if _HYBRID.search(blob):
        return "hybrid"
    if _REMOTE.search(blob):
        return "remote"
    return "onsite" if blob.strip() else "unknown"


def parse_dt(value) -> str:
    """Anything a job board calls a date -> 'YYYY-MM-DD', or '' if unparseable."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z", "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(text.replace("Z", "+0000"), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def job(company, title, url, *, location="", posted_at="", source="",
        description="", raw_seniority="", workplace="", external_id="") -> dict:
    title = (title or "").strip()
    company = (company or "").strip()
    url = (url or "").strip()
    return {
        "uid": make_uid(company, title, url, external_id),
        "company": company,
        "title": title,
        "url": url,
        "location": (location or "").strip(),
        # Location only, never the title. Feeding the title in here routed any
        # job NAMED for one of _REMOTE's words down the remote branch of the
        # location gate: "Remote Sensing Data Scientist", "ML Engineer,
        # Distributed Training" and "AI Engineer (Work From Home Friendly)"
        # were all marked remote and then dropped despite Istanbul locations.
        "workplace": workplace or guess_workplace(location),
        "posted_at": posted_at,
        "source": source,
        # `or ""`: bamboohr's departmentLabel comes back null on some postings,
        # which used to be a TypeError here that killed that whole fetcher.
        "description": (description or "")[:8000],
        "raw_seniority": raw_seniority,
        "external_id": (external_id or "").strip(),
    }


# ------------------------------------------------------------ group A: ATS APIs

def fetch_greenhouse(token, company, c):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    data = c.get(url).raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        out.append(job(
            company, j.get("title"), j.get("absolute_url"),
            location=(j.get("location") or {}).get("name", ""),
            # first_published FIRST: updated_at is the last-edit timestamp, so
            # it reads as "posted days ago" for every job on an actively curated
            # board (gitlab: 100% within 30d on updated_at vs 44% on the real
            # date) and hands the whole board a recency bonus it didn't earn.
            posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            source=f"greenhouse:{token}",
            description=strip_html(j.get("content")),
        ))
    return out


def fetch_lever(token, company, c):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = c.get(url).raise_for_status().json()
    out = []
    for j in data:
        cat = j.get("categories") or {}
        # descriptionPlain is only the posting INTRO. The responsibilities and
        # requirements bullets - where every skill keyword actually lives - are
        # in descriptionBodyPlain and lists[].content, and dropping them left
        # lever:dreamgames scoring a mean skill weight of 0.00 across 20 jobs
        # while greenhouse averaged 2.91. Measured 1287 of 3806 available chars
        # on lever:trendyol's first posting (2026-09-14).
        parts = [j.get("descriptionPlain") or strip_html(j.get("description")),
                 j.get("descriptionBodyPlain") or "",
                 *(strip_html(l.get("content")) for l in (j.get("lists") or []))]
        out.append(job(
            company, j.get("text"), j.get("hostedUrl") or j.get("applyUrl"),
            location=cat.get("location", ""),
            posted_at=parse_dt(j.get("createdAt")),
            source=f"lever:{token}",
            description=" ".join(p for p in parts if p),
            raw_seniority=cat.get("commitment", ""),
            # workplaceType is a TOP-LEVEL key, not one of `categories`. Read off
            # `cat` it was always "", so all 23 Trendyol postings came back
            # "onsite" when the board says 18 hybrid / 5 onsite - costing the
            # hybrid bonus and pushing them down the Filter's stricter onsite
            # location branch.
            workplace=j.get("workplaceType") or guess_workplace(cat.get("location", "")),
        ))
    return out


def fetch_ashby(token, company, c):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    data = c.get(url).raise_for_status().json()
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location") or ""
        out.append(job(
            company, j.get("title"), j.get("jobUrl"),
            location=loc,
            posted_at=parse_dt(j.get("publishedAt")),
            source=f"ashby:{token}",
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            raw_seniority=j.get("employmentType", ""),
            workplace="remote" if j.get("isRemote") else guess_workplace(loc),
        ))
    return out


def fetch_workable(token, company, c):
    url = f"https://apply.workable.com/api/v3/accounts/{token}/jobs"
    out = []
    # ONE request. Workable v3 rejects limit/offset in the body outright - it
    # answers HTTP 400 {"limit":"Not allowed"}, so this fetcher failed 100% of
    # the time on every account; {"query": ""} alone returns 200. Looping on it
    # re-fetched the identical page and counted the duplicates as progress, so
    # an account with 100 jobs and 20 per response reported itself COMPLETE
    # after 5 passes - and a complete source gets swept, closing the 80 live
    # jobs it never saw.
    # ponytail: no pagination, because the endpoint offers none through this
    # body. If an account's board outgrows one response, paginate with whatever
    # cursor the 200 actually carries - don't guess at it again.
    r = c.post(url, json={"query": ""})
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or data.get("jobs") or []
    for j in results:
        loc = j.get("location") or {}
        loc_text = ", ".join(
            x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
        ) if isinstance(loc, dict) else str(loc)
        shortcode = j.get("shortcode") or j.get("id") or ""
        out.append(job(
            company, j.get("title"),
            j.get("url") or j.get("application_url")
            or f"https://apply.workable.com/{token}/j/{shortcode}/",
            location=loc_text,
            posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
            source=f"workable:{token}",
            description=strip_html(j.get("description")),
            raw_seniority=j.get("employment_type", ""),
            workplace={"on_site": "onsite"}.get(j.get("workplace", ""), j.get("workplace", ""))
            or guess_workplace(loc_text),
        ))
    total = int(data.get("total") or 0)
    if total > len(out):
        note_truncated(f"workable:{token}", len(out), total)
    return out


def fetch_smartrecruiters(token, company, c):
    out, offset = [], 0
    for _ in range(MAX_PAGES):
        url = (f"https://api.smartrecruiters.com/v1/companies/{token}"
               f"/postings?limit=100&offset={offset}")
        data = c.get(url).raise_for_status().json()
        content = data.get("content", [])
        if not content:
            break
        for j in content:
            loc = j.get("location") or {}
            loc_text = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
            out.append(job(
                company, j.get("name"),
                f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                location=loc_text,
                # releasedDate is a re-release timestamp, not the posting date:
                # two independent boards came back 100% within 7 days with zero
                # spread, so every posting collected the full recency bonus.
                # Same shape as the himalayas feed-timestamp bug. "" is neutral.
                posted_at="",
                source=f"smartrecruiters:{token}",
                # ponytail: always "". The /postings LIST endpoint does not
                # return jobAd (only a defaultJobAd id) - measured 0 of 300
                # postings across two companies - so these rows carry no body
                # and cannot score. Real text needs /postings/{id}, one request
                # per job; worth it only if a company here actually uses it.
                description=strip_html((j.get("jobAd") or {}).get("sections", {}).get(
                    "jobDescription", {}).get("text", "")),
                raw_seniority=(j.get("experienceLevel") or {}).get("id", ""),
                workplace="remote" if loc.get("remote") else guess_workplace(loc_text),
            ))
        offset += len(content)
        if offset >= int(data.get("totalFound") or 0):
            break
    else:
        # for/else: reached only when the loop was never broken out of, i.e. the
        # page cap ran out while totalFound still promised more.
        note_truncated(f"smartrecruiters:{token}", len(out),
                       data.get("totalFound", "?"))
    return out


def fetch_recruitee(token, company, c):
    url = f"https://{token}.recruitee.com/api/offers/"
    data = c.get(url).raise_for_status().json()
    out = []
    for j in data.get("offers", []):
        loc = j.get("location") or ", ".join(
            x for x in (j.get("city"), j.get("country")) if x)
        out.append(job(
            company, j.get("title"), j.get("careers_url") or j.get("url"),
            location=loc,
            posted_at=parse_dt(j.get("published_at") or j.get("created_at")),
            source=f"recruitee:{token}",
            description=strip_html(f"{j.get('description','')} {j.get('requirements','')}"),
            raw_seniority=j.get("employment_type_code", ""),
            workplace="remote" if j.get("remote") else guess_workplace(loc),
        ))
    return out


def fetch_personio(token, company, c):
    url = f"https://{token}.jobs.personio.de/xml"
    root = ET.fromstring(c.get(url).raise_for_status().content)
    out = []
    for pos in root.iter("position"):
        def txt(tag):
            el = pos.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        desc = " ".join(
            (v.text or "") for v in pos.iter("value") if v.text)
        loc = txt("office")
        out.append(job(
            company, txt("name"),
            f"https://{token}.jobs.personio.de/job/{txt('id')}",
            location=loc,
            posted_at=parse_dt(txt("createdAt")),
            source=f"personio:{token}",
            description=strip_html(desc),
            raw_seniority=f"{txt('seniority')} {txt('yearsOfExperience')}".strip(),
            workplace=guess_workplace(loc, txt("schedule")),
        ))
    return out


def _rss_items(xml_bytes, company, source, url_from_link=True):
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        def txt(tag):
            # Match on the local name: Teamtailor puts its useful elements in a
            # namespace ({ns}locations, {ns}department), and a plain find()
            # silently missed every one of them.
            for el in item:
                if el.tag.rpartition("}")[2] == tag:
                    return (el.text or "").strip() if el.text else ""
            return ""
        desc = strip_html(txt("description"))
        # NOT desc[:80]. There is no <location> element in a Teamtailor feed,
        # so the old fallback made the first 80 characters of the job body the
        # location for 73% of postings - strings like "The opportunity We are
        # currently looking for an Inside Sales Advisor to join our" were then
        # regex-matched for cities and fed to guess_workplace, so a body that
        # happened to open with "work from home" silently scored the remote
        # bonus. An unknown location is "".
        remote = txt("remoteStatus")
        out.append(job(
            company, txt("title"), txt("link"),
            location=txt("location") or txt("locations"),
            posted_at=parse_dt(txt("pubDate")),
            source=source,
            description=desc,
            workplace="remote" if remote.lower() in ("fully", "temporary") else "",
        ))
    return out


def fetch_teamtailor(token, company, c):
    url = f"https://{token}.teamtailor.com/jobs.rss"
    return _rss_items(c.get(url).raise_for_status().content, company, f"teamtailor:{token}")


def fetch_bamboohr(token, company, c):
    url = f"https://{token}.bamboohr.com/careers/list"
    data = c.get(url).raise_for_status().json()
    out = []
    for j in (data.get("result") or []):
        loc = j.get("location") or {}
        loc_text = ", ".join(
            x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x
        ) if isinstance(loc, dict) else str(loc)
        out.append(job(
            company, j.get("jobOpeningName"),
            f"https://{token}.bamboohr.com/careers/{j.get('id')}",
            location=loc_text,
            posted_at=parse_dt(j.get("datePosted")),
            source=f"bamboohr:{token}",
            # ponytail: a department name, not a description - ~10 chars against
            # greenhouse's 8000, so these cannot score on skills. The real body
            # is on the per-job detail page, one request each.
            description=j.get("departmentLabel", ""),
            raw_seniority=j.get("employmentStatusLabel", ""),
            workplace="remote" if j.get("isRemote") else guess_workplace(loc_text),
        ))
    return out


def fetch_workday(token, company, c):
    """token format: 'tenant|wd3|SiteName'."""
    try:
        tenant, wd, site = token.split("|")
    except ValueError:
        raise ValueError(f"workday token must be 'tenant|wd3|Site', got {token!r}")
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}"
    out, offset, total = [], 0, 0
    for _ in range(MAX_PAGES):
        r = c.post(api, json={"appliedFacets": {}, "limit": 20,
                              "offset": offset, "searchText": ""})
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        # `total` is only populated on the FIRST page - later pages report 0,
        # so comparing against it each time made `20 >= 0` true and stopped
        # after page 2. NVIDIA returned 40 of 2000 jobs and looked healthy.
        total = total or int(data.get("total") or 0)
        for j in postings:
            # Workday pads responses with placeholder rows carrying nothing but
            # a requisition id; they became listings titled "".
            if not j.get("title"):
                continue
            loc = j.get("locationsText", "")
            out.append(job(
                company, j.get("title"), base + (j.get("externalPath") or ""),
                location=loc,
                # ponytail: no date. There is no startDate in the cxs response
                # (it was always ""); the real field is `postedOn`, relative
                # prose like "Posted 2 Days Ago" that parse_dt can't read. Emit
                # nothing rather than a fabricated date - "" scores neutral.
                posted_at="",
                source=f"workday:{token}",
                # ponytail: bulletFields is the requisition ID, ~9 chars, not a
                # description - these postings cannot score on skills. The body
                # needs the per-job cxs detail endpoint (one request each).
                description=" ".join(j.get("bulletFields") or []),
                workplace=guess_workplace(loc),
            ))
        offset += len(postings)
        if total and offset >= total:
            break
    else:
        note_truncated(f"workday:{token}", len(out), total or "?")
    return out


def _walk_json_ld(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json_ld(item)
    elif isinstance(value, dict):
        if value.get("@type") == "JobPosting" or "JobPosting" in (value.get("@type") or []):
            yield value
        for item in value.get("@graph", []):
            yield from _walk_json_ld(item)


def _text_values(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _text_values(item)]
    if isinstance(value, list):
        return [text for item in value for text in _text_values(item)]
    return []


def fetch_html_jobs(url: str, company: str, source: str, c: httpx.Client,
                    *, detail_limit: int = 60) -> list[dict]:
    """Public HTML fallback for ATSs without a stable unauthenticated API.

    JSON-LD is preferred because it carries the complete JobPosting schema.
    For portals that only expose cards, follow public vacancy links and use the
    detail page body. A deliberately bounded detail walk cannot be mistaken for
    a complete source: `note_truncated` protects the closing sweep.
    """
    response = c.get(url)
    response.raise_for_status()
    tree = HTMLParser(response.text)
    out: list[dict] = []
    seen: set[str] = set()
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except (TypeError, ValueError):
            continue
        for item in _walk_json_ld(data):
            title = item.get("title") or item.get("name") or ""
            posting_url = item.get("url") or url
            ident = item.get("identifier") or ""
            if isinstance(ident, dict):
                ident = ident.get("value") or ident.get("name") or ""
            location = item.get("jobLocation") or item.get("applicantLocationRequirements") or ""
            if isinstance(location, (list, dict)):
                location = " ".join(_text_values(location))
            out.append(job(company, title, posting_url, location=str(location),
                           posted_at=parse_dt(item.get("datePosted")), source=source,
                           description=strip_html(item.get("description")),
                           workplace=guess_workplace(str(location), str(item.get("jobLocationType") or "")),
                           external_id=str(ident)))
            seen.add(canonical_url(posting_url))
    if out:
        return out

    links = []
    for anchor in tree.css("a[href]"):
        href = urljoin(url, anchor.attributes.get("href", ""))
        label = " ".join(anchor.text(separator=" ").split())
        path = urlsplit(href).path.lower()
        if (not href.startswith(("http://", "https://")) or canonical_url(href) in seen
                or len(label) < 4 or len(label) > 160
                or not re.search(r"job|position|career|ilan|vacanc|role", path, re.I)):
            continue
        seen.add(canonical_url(href))
        links.append((label, href))
    if len(links) > detail_limit:
        note_truncated(source, detail_limit, len(links), detail_limit)
        links = links[:detail_limit]
    for title, detail_url in links:
        detail = c.get(detail_url)
        detail.raise_for_status()
        detail_tree = HTMLParser(detail.text)
        body = strip_html(detail_tree.body.text(separator=" ") if detail_tree.body else detail.text)
        # Nearby visible text is more reliable than inferring a city from a
        # careers-site headquarters footer, so leave unknown locations unknown.
        out.append(job(company, title, detail_url, source=source, description=body,
                       workplace=guess_workplace(body)))
    return out


def fetch_careers_page(token, company, c, board=None):
    url = (board or {}).get("careers_url") or f"https://www.careers-page.com/{token}"
    return fetch_html_jobs(url, company, f"careers-page:{token}", c)


def fetch_manatal(token, company, c, board=None):
    url = (board or {}).get("careers_url") or f"https://www.careers-page.com/{token}"
    return fetch_html_jobs(url, company, f"manatal:{token}", c)


def fetch_jobvite(token, company, c, board=None):
    url = (board or {}).get("careers_url") or f"https://jobs.jobvite.com/{token}/jobs"
    return fetch_html_jobs(url, company, f"jobvite:{token}", c)


def fetch_successfactors(token, company, c, board=None):
    url = (board or {}).get("careers_url") or (token if token.startswith("http") else "")
    if not url:
        raise ValueError("successfactors requires a public careers_url")
    return fetch_html_jobs(url, company, f"successfactors:{token}", c)


def fetch_custom(token, company, c, board=None):
    url = (board or {}).get("careers_url") or token
    if not url.startswith(("http://", "https://")):
        raise ValueError("custom portal requires a public careers_url")
    source = (board or {}).get("source") or f"custom:{company.lower().replace(' ', '-') }"
    return fetch_html_jobs(url, company, source, c)


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "teamtailor": fetch_teamtailor,
    "bamboohr": fetch_bamboohr,
    "workday": fetch_workday,
    "careers-page": fetch_careers_page,
    "manatal": fetch_manatal,
    "successfactors": fetch_successfactors,
    "jobvite": fetch_jobvite,
    "custom": fetch_custom,
}


def fetch_company(entry: dict, c: httpx.Client) -> list[dict]:
    ats, token = entry.get("ats"), entry.get("token")
    if ats in (None, "unknown") or not token:
        return []
    fetcher = ATS_FETCHERS.get(ats)
    if fetcher is None:
        raise ValueError(f"unknown ats {ats!r} for {entry.get('name')}")
    try:
        return fetcher(token, entry["name"], c, entry)
    except TypeError as exc:
        # Established API adapters deliberately retain their concise
        # (token, company, client) signatures; fallback portals also receive
        # their catalogue metadata for public careers URLs.
        if "positional" not in str(exc):
            raise
        return fetcher(token, entry["name"], c)


# ----------------------------------------------------------- group B: job boards

def fetch_remotive(c):
    data = c.get("https://remotive.com/api/remote-jobs").raise_for_status().json()
    return [
        job(j.get("company_name"), j.get("title"), j.get("url"),
            location=j.get("candidate_required_location", ""),
            posted_at=parse_dt(j.get("publication_date")),
            source="remotive",
            description=strip_html(j.get("description")) + " " + " ".join(j.get("tags") or []),
            raw_seniority=j.get("job_type", ""),
            workplace="remote")
        for j in data.get("jobs", [])
    ]


def unmojibake(s: str | None) -> str:
    """Repair UTF-8 that was already decoded as latin-1 upstream.

    remoteok serves 45 of its ~99 postings this way: "دبي" arrives as "Ø¯Ø¨Ù".
    Descriptions are affected too, so the Turkish reject patterns
    (pazarlama, içerik, müşteri) silently cannot match on that source.
    Text that is not a clean latin-1 round trip is genuine and left alone.
    """
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, AttributeError):
        return s or ""


def fetch_remoteok(c):
    data = c.get("https://remoteok.com/api").raise_for_status().json()
    return [
        job(unmojibake(j.get("company")), unmojibake(j.get("position")),
            j.get("url") or j.get("apply_url"),
            location=unmojibake(j.get("location")) or "Remote",
            posted_at=parse_dt(j.get("epoch") or j.get("date")),
            source="remoteok",
            description=unmojibake(
                strip_html(j.get("description")) + " " + " ".join(j.get("tags") or [])),
            workplace="remote")
        for j in data if isinstance(j, dict) and j.get("position")
    ]


def fetch_himalayas(c):
    out, cursor = [], None
    cutoff = time.time() - HIMALAYAS_DAYS * 86400
    for n in range(HIMALAYAS_MAX_PAGES):
        url = "https://himalayas.app/jobs/api?limit=100" + (f"&cursor={cursor}" if cursor else "")
        if n:
            time.sleep(HIMALAYAS_PAGE_DELAY)
        r = c.get(url)
        if r.status_code == 429:
            # Rate limited mid-walk. Measured live: the feed starts answering 429
            # somewhere past a hundred pages. Keep the jobs already in hand and
            # stop - raising here would lose all of them and mark the source
            # failed, when what actually happened is a short read.
            note_truncated("himalayas", len(out), "more (rate limited at 429)",
                           HIMALAYAS_MAX_PAGES)
            return out
        data = r.raise_for_status().json()
        jobs = data.get("jobs", [])
        if not jobs:
            break
        for j in jobs:
            restrictions = j.get("locationRestrictions") or []
            out.append(job(
                j.get("companyName"), j.get("title"),
                j.get("applicationLink") or j.get("guid"),
                location=", ".join(restrictions) if restrictions else "Worldwide",
                # pubDate is when himalayas published the row, not when the
                # employer posted the job, so it is not a posting date and is
                # not emitted as one - a shallow read handed all 240 listings
                # the full recency bonus (verified live 2026-09-14), and the
                # scorer already treats "" as neutral. It IS a reliable
                # descending feed order, which is what the window above walks.
                posted_at="",
                source="himalayas",
                description=strip_html(j.get("description") or j.get("excerpt")),
                raw_seniority=" ".join(j.get("seniority") or []),
                workplace="remote"))
        cursor = data.get("nextCursor")
        if not cursor:
            break
        # The feed is ordered newest-first, so the last job on the page is the
        # oldest seen so far. Past the window, everything below is older still.
        # Only real timestamps count: a single job with a missing pubDate would
        # otherwise drag the page minimum to 0 and silently disable the window,
        # which is what walked 6720 jobs into a 429 the first time this ran.
        stamps = [j["pubDate"] for j in jobs if j.get("pubDate")]
        if stamps and min(stamps) < cutoff:
            break
    else:
        # Not "the feed ran out" but "the backstop fired before the window
        # closed" - the run saw less than HIMALAYAS_DAYS and must not be swept.
        note_truncated("himalayas", len(out),
                       f"more (page cap hit before {HIMALAYAS_DAYS}d window closed)",
                       HIMALAYAS_MAX_PAGES)
    return out


WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def fetch_weworkremotely(c):
    out = []
    for feed in WWR_FEEDS:
        root = ET.fromstring(c.get(feed).raise_for_status().content)
        for item in root.iter("item"):
            def txt(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            # WWR encodes "Company: Title" in <title>
            title_raw = txt("title")
            company, _, title = title_raw.partition(":")
            if not title:
                company, title = "", title_raw
            out.append(job(
                company.strip(), title.strip(), txt("link"),
                location=txt("region") or "Remote",
                posted_at=parse_dt(txt("pubDate")),
                source="weworkremotely",
                description=strip_html(txt("description")),
                workplace="remote"))
    return out


def fetch_techcareer(c):
    """techcareer.net's job list is paginated client-side against its own bff
    API, found via live network inspection (2026-09-14): the server-rendered
    __NEXT_DATA__ blob and its /_next/data route both always return page 1
    regardless of query string, but this endpoint is a plain unauthenticated
    GET and returns the full HTML job description too (no per-job detail
    fetch needed).

    # ponytail: found by watching network traffic while clicking "page 2" in
    # a throwaway Playwright session, not documented anywhere - if
    # techcareer.net changes it, this breaks with a clear "0 items" via the
    # empty-page break below, not a silent wrong answer.
    """
    out = []
    for page in range(1, MAX_PAGES + 1):
        url = ("https://www.techcareer.net/api/bff/jobs/job-list"
               f"?jobs[isCompleted]=false&jobs[page]={page}")
        data = c.get(url).raise_for_status().json()
        items = data.get("jobs") or []
        if not items:
            break
        for j in items:
            title = j.get("title") or ""
            slug = j.get("slug") or ""
            # techcareer allows confidential postings (company: null) - name the
            # gap rather than rendering an empty cell in the report.
            company = (j.get("company") or {}).get("companyProfileName") or "(undisclosed)"
            loc = (j.get("location") or {}).get("locationName") or ""
            other_locs = " ".join(
                l.get("locationName", "") for l in j.get("otherLocations") or [])
            places = " ".join(
                w.get("workPlaceName", "") for w in j.get("workPlaces") or [])
            out.append(job(
                company, title,
                j.get("applyLink") or f"https://www.techcareer.net/is-ilanlari/{slug}",
                location=" ".join(filter(None, [loc, other_locs])),
                source="techcareer",
                description=" ".join(filter(None, [
                    strip_html(j.get("description")), j.get("jobTitleNameEn")])),
                workplace=guess_workplace(loc, places)))
        # A missing pageCount must NOT end the loop - `page >= page` would be
        # true on page 1 and silently return a fifth of the board. Fall through
        # to the empty-page break instead, which is the loud failure mode the
        # docstring promises.
        page_count = data.get("pageCount")
        if page_count and page >= page_count:
            break
    else:
        note_truncated("techcareer", len(out), f"{page_count or '?'} pages")
    return out


def fetch_youthall(c):
    """youthall.com - Turkish new-grad / internship board.
    Server-rendered HTML, cards are div.jobs (verified live 2026-09-14)."""
    out = []
    # youthall's ?page= runs out of content without saying so: pages 3 and 4
    # come back byte-identical to each other and every href on them is already
    # on page 1, so the board's 36 real jobs were reported as 60. `cards` is
    # never empty, so the loop below can only end by running out of new hrefs.
    seen: set[str] = set()
    for page in range(1, 5):
        url = f"https://www.youthall.com/tr/is-ilanlari/?page={page}"
        tree = HTMLParser(c.get(url).raise_for_status().text)
        cards = tree.css("div.jobs")
        if not cards:
            break
        before = len(seen)
        for card in cards:
            a = card.css_first("a")
            href = a.attributes.get("href", "") if a else ""
            if not href or href in seen:
                continue
            seen.add(href)
            h5 = card.css_first("h5")
            title = h5.text().strip() if h5 else ""
            logo = card.css_first("img.jobs-content-logo")
            company = (logo.attributes.get("alt") or "").removesuffix(" logo").strip() \
                if logo else ""
            tags = [t.text(separator=" ").strip() for t in card.css(".jobs-content-bottom > div")]
            job_type = tags[0] if len(tags) > 0 else ""
            loc = " ".join((tags[2] if len(tags) > 2 else "").split())
            desc_el = card.css_first(".jobs-content-desc")
            desc = desc_el.text().strip() if desc_el else ""
            out.append(job(
                company, title, href,
                location=loc, source="youthall",
                description=f"{desc} {job_type}",
                raw_seniority=job_type,
                workplace=guess_workplace(loc, job_type)))
        if len(seen) == before:
            break
    return out


BOARD_FETCHERS = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "himalayas": fetch_himalayas,
    "weworkremotely": fetch_weworkremotely,
    "techcareer": fetch_techcareer,
    "youthall": fetch_youthall,
}
